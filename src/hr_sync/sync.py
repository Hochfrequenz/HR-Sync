"""
The orchestration of a run: load the systems, create the plan, execute it.

Each step is a function of its own, so that it can be called and debugged on its own, e.g. by an integration test
that executes only one entry of the plan:

- :func:`connect` creates the clients of the systems from the settings, without IO.
- :func:`load_snapshots` loads Personio and the selected target systems, one after the other.
- :func:`create_plan` creates the plan and logs it.
- :func:`execute_plan` executes it, first in weclapp, then in Decidalo.

:func:`run` does all of it and logs the event ``sync.run``. Any error ends the run with :attr:`ExitCode.ABORTED`.
"""

from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import IntEnum
from uuid import uuid4

from decidalo_client import DecidaloClient
from personio_client import PersonioClient
from pydantic import SecretStr

from hr_sync.config import ConfigurationError, ConfigurationProblem, Settings, check_settings
from hr_sync.events import emit, emit_operation, error_fields, run_context, wide_event
from hr_sync.model import Notice, Snapshot, TargetSystem
from hr_sync.plan import Operation, OperationResult, Outcome, SyncPlan
from hr_sync.planner import build_plan
from hr_sync.systems.decidalo import DecidaloTarget, execute_decidalo, load_decidalo
from hr_sync.systems.personio import PersonioSource, load_personio


class ExitCode(IntEnum):
    """The exit codes of HR-Sync."""

    SUCCESS = 0
    """also with an empty plan or with notices"""
    OPERATION_FAILED = 1
    """at least one operation failed or was skipped"""
    ABORTED = 2
    """e.g. because of the configuration or an error while loading"""


@dataclass(frozen=True)
class RunOptions:
    """The options of a run, as given on the command line."""

    targets: tuple[TargetSystem, ...] = tuple(TargetSystem)
    """the selected target systems; the others are neither loaded nor written"""
    only: frozenset[str] | None = None
    """restricts the plan to these employee numbers; all employees are loaded anyway, to find data problems"""
    max_employees: int | None = None
    """stops planning as soon as this many employees have operations, e.g. to keep a test run small"""
    dry_run: bool = False


@dataclass(frozen=True)
class Systems:
    """The clients of the systems. A target system that is not selected has no client."""

    personio: PersonioSource
    decidalo: DecidaloTarget | None = None


@dataclass(frozen=True)
class Snapshots:
    """The loaded snapshots of a run."""

    source: Snapshot
    targets: dict[TargetSystem, Snapshot]
    """only the selected target systems"""

    @property
    def notices(self) -> list[Notice]:
        """
        Returns the notices of all snapshots, in the order in which the systems were loaded.
        """
        return [*self.source.notices, *(notice for target in self.targets.values() for notice in target.notices)]


def run(
    settings: Settings, options: RunOptions, *, systems: Systems | None = None, today: date | None = None
) -> ExitCode:
    """
    Runs the sync and returns its exit code. Logs the event ``sync.run`` with the totals of the operations.

    ``systems`` defaults to the clients created by :func:`connect`, ``today`` to ``date.today()``.
    """
    with (
        run_context(run_id=uuid4().hex, environment=settings.hr_sync_environment, dry_run=options.dry_run),
        wide_event("sync.run") as event,
    ):
        event.update(_option_fields(options))
        try:
            check_settings(settings, options.targets)
            if systems is None:
                systems = connect(settings, options.targets)
            snapshots = load_snapshots(systems, settings, options.targets, today or date.today())
            sync_plan = create_plan(snapshots, options.only, options.max_employees)
            results = execute_plan(systems, sync_plan, settings, dry_run=options.dry_run)
        except Exception as error:  # any error ends the run with an event, but without its message
            return _aborted(event, error)
        exit_code = _exit_code(results)
        event["results"] = _totals(results)
        event["notices"] = len(snapshots.notices) + len(sync_plan.notices)
        event["errors"] = sum(result.outcome == Outcome.ERROR for result in results)
        event["exit_code"] = int(exit_code)
        if exit_code != ExitCode.SUCCESS:
            event["outcome"] = "error"
        return exit_code


def abort(options: RunOptions, error: Exception) -> ExitCode:
    """
    Logs the event ``sync.run`` of a run that can't even start, e.g. because the settings can't be read, and returns
    :attr:`ExitCode.ABORTED`.
    """
    with (
        run_context(run_id=uuid4().hex, dry_run=options.dry_run),
        wide_event("sync.run") as event,
    ):
        event.update(_option_fields(options))
        return _aborted(event, error)


def connect(settings: Settings, targets: Collection[TargetSystem]) -> Systems:
    """
    Creates the clients of Personio and the selected target systems, without IO. Decidalo is always the production
    system: its URL is the default of the client and can't be configured.

    Raises:
        ConfigurationError: if a credential is missing, which :func:`check_settings` reports beforehand.
    """
    personio = PersonioClient(
        client_id=_secret(settings.personio_client_id, "PERSONIO_CLIENT_ID"),
        client_secret=_secret(settings.personio_client_secret, "PERSONIO_CLIENT_SECRET"),
        app_id=settings.personio_app_id,
    )
    decidalo = None
    if TargetSystem.DECIDALO in targets:
        decidalo = DecidaloClient(api_key=_secret(settings.decidalo_api_key, "DECIDALO_API_KEY"))
    return Systems(personio=personio, decidalo=decidalo)


def load_snapshots(systems: Systems, settings: Settings, targets: Collection[TargetSystem], today: date) -> Snapshots:
    """
    Loads Personio and the selected target systems, one after the other, and logs the notices of each snapshot.

    An error while loading is raised, so that the run ends before planning: on incomplete data, existing employees
    would be created again.
    """
    source = load_personio(
        systems.personio, employee_number_attribute=settings.personio_employee_number_attribute, today=today
    )
    _emit_notices(source.notices)
    loaded: dict[TargetSystem, Snapshot] = {}
    for target in TargetSystem:
        if target not in targets:
            continue
        if target == TargetSystem.DECIDALO:
            loaded[target] = load_decidalo(_decidalo(systems))
        else:
            raise NotImplementedError(f"HR-Sync can't load {target} yet")
        _emit_notices(loaded[target].notices)
    return Snapshots(source=source, targets=loaded)


def create_plan(
    snapshots: Snapshots, only: Collection[str] | None = None, max_employees: int | None = None
) -> SyncPlan:
    """
    Creates the plan (see :func:`hr_sync.planner.build_plan`) and logs its notices, one event ``sync.deviation`` per
    employee and target system with deviations, and the event ``sync.plan``.
    """
    with wide_event("sync.plan") as event:
        sync_plan = build_plan(snapshots.source, snapshots.targets, only=only, max_employees=max_employees)
        _emit_notices(sync_plan.notices)
        deviations: Counter[TargetSystem] = Counter()
        for employee in sync_plan.employees:
            for deviation in employee.deviations:
                deviations[deviation.system] += 1
                emit(
                    "sync.deviation",
                    employee_number=employee.employee_number,
                    system=deviation.system,
                    fields=list(deviation.fields),
                )
        event["operations"] = {
            target.value: dict(Counter(operation.type.value for operation in sync_plan.operations_for(target)))
            for target in snapshots.targets
        }
        event["deviations"] = {target.value: deviations[target] for target in snapshots.targets}
        event["only_in_target"] = {target.value: count for target, count in sync_plan.only_in_target.items()}
        event["notices"] = len(sync_plan.notices)
        event["is_empty"] = sync_plan.is_empty
        event["limit_reached"] = sync_plan.limit_reached
    return sync_plan


def execute_plan(systems: Systems, sync_plan: SyncPlan, settings: Settings, *, dry_run: bool) -> list[OperationResult]:
    """
    Executes the plan, first in weclapp, then in Decidalo. Each executor logs one event ``sync.operation`` per
    operation. If a target system fails as a whole, its executor skips its remaining operations, and the next target
    system is executed anyway.

    In a dry run, nothing is written: every operation gets the outcome ``dry_run``.
    """
    results: list[OperationResult] = []
    for target in TargetSystem:
        operations = sync_plan.operations_for(target)
        if not operations:
            continue
        if dry_run:
            results.extend(_dry_run(operations))
        elif target == TargetSystem.DECIDALO:
            results.extend(
                execute_decidalo(
                    _decidalo(systems),
                    operations,
                    employee_type_name=settings.decidalo_employee_type_name,
                    has_login=settings.decidalo_has_login,
                )
            )
        else:
            raise NotImplementedError(f"HR-Sync can't execute operations in {target} yet")
    return results


def _dry_run(operations: Iterable[Operation]) -> list[OperationResult]:
    results = [OperationResult(operation=operation, outcome=Outcome.DRY_RUN) for operation in operations]
    for result in results:
        emit_operation(result)
    return results


def _decidalo(systems: Systems) -> DecidaloTarget:
    if systems.decidalo is None:
        raise ValueError("Decidalo is a selected target system, but has no client")
    return systems.decidalo


def _secret(value: SecretStr | None, variable: str) -> str:
    if value is None:
        raise ConfigurationError(ConfigurationProblem.MISSING_VARIABLES, [variable])
    return value.get_secret_value()


def _emit_notices(notices: Iterable[Notice]) -> None:
    for notice in notices:
        emit("sync.notice", level="WARNING", **notice.model_dump(mode="json", exclude_none=True))


def _option_fields(options: RunOptions) -> dict[str, object]:
    fields: dict[str, object] = {"targets": [target.value for target in options.targets]}
    if options.only is not None:
        fields["only"] = sorted(options.only)
    if options.max_employees is not None:
        fields["max_employees"] = options.max_employees
    return fields


def _aborted(event: dict[str, object], error: Exception) -> ExitCode:
    fields = error_fields(error)
    if isinstance(error, ConfigurationError):
        fields.update(error.fields)
    event["outcome"] = "error"
    event["error"] = fields
    event["exit_code"] = int(ExitCode.ABORTED)
    return ExitCode.ABORTED


def _exit_code(results: Sequence[OperationResult]) -> ExitCode:
    if any(result.outcome in (Outcome.ERROR, Outcome.SKIPPED) for result in results):
        return ExitCode.OPERATION_FAILED
    return ExitCode.SUCCESS


def _totals(results: Iterable[OperationResult]) -> dict[str, dict[str, dict[str, int]]]:
    """
    Returns the number of operations per target system, operation type and outcome.
    """
    totals: dict[str, dict[str, dict[str, int]]] = {}
    for result in results:
        by_outcome = totals.setdefault(result.operation.system.value, {}).setdefault(result.operation.type.value, {})
        by_outcome[result.outcome.value] = by_outcome.get(result.outcome.value, 0) + 1
    return totals
