from collections.abc import Callable
from datetime import date
from unittest.mock import ANY

import pytest
from decidalo_client import DecidaloClient
from decidalo_client.models import ImportItemStatusType, UserImportResults
from personio_client import Employment, Person, PersonioClient
from pydantic import SecretStr

from hr_sync.config import ConfigurationError, ConfigurationProblem, Environment, Settings
from hr_sync.model import EmployeeRecord, System, TargetSystem
from hr_sync.plan import EmployeePlan, Operation, OperationType, Outcome, SyncPlan
from hr_sync.sync import (
    ExitCode,
    RunOptions,
    Systems,
    connect,
    create_plan,
    execute_plan,
    load_snapshots,
    run,
)
from unittests.fakes import (
    ATTRIBUTE,
    FakeDecidalo,
    FakePersonio,
    decidalo_user,
    import_row,
    personio_employment,
    personio_person,
)

TODAY = date(2026, 10, 29)
DECIDALO = RunOptions(targets=(TargetSystem.DECIDALO,))
"""a run with Decidalo as the only target, as long as the weclapp adapter is missing"""

ERIKA = personio_person("1", "00165")
MAX = personio_person("2", "00200", email="max.mustermann@example.com", first_name="Max", last_name="Mustermann")
START = date(2026, 9, 1)
"""the start of the employments in Personio"""
MAX_IN_DECIDALO = decidalo_user(
    7, "00200", email="max.mustermann@example.com", display_name="Max Mustermann", entry_date=START
)
PERSONAL_DATA = ("Erika", "Musterfrau", "Max", "Mustermann", "@example.com")


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "personio_client_id": SecretStr("papi-id"),
        "personio_client_secret": SecretStr("papi-secret"),
        "decidalo_api_key": SecretStr("decidalo-key"),
    }
    return Settings.model_validate(values | changes)


def _personio(*persons: Person, employments: list[Employment] | None = None) -> FakePersonio:
    return FakePersonio([(person, employments or [personio_employment()]) for person in persons])


def _run(
    personio: FakePersonio, decidalo: FakeDecidalo, options: RunOptions = DECIDALO, settings: Settings | None = None
) -> ExitCode:
    systems = Systems(personio=personio, decidalo=decidalo)
    return run(settings or _settings(), options, systems=systems, today=TODAY)


def _named(events: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [event for event in events if event["event"] == name]


def _run_event(events: list[dict[str, object]]) -> dict[str, object]:
    (event,) = _named(events, "sync.run")
    return event


class TestRun:
    """
    Tests of whole runs against fake clients.
    """

    def test_creates_the_missing_employees(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo(users=[MAX_IN_DECIDALO])
        assert _run(_personio(ERIKA, MAX), decidalo) == ExitCode.SUCCESS
        assert [[user.employeeID for user in batch] for batch in decidalo.batches] == [["00165"]]
        assert [(event["event"], event.get("system")) for event in events] == [
            ("sync.load", System.PERSONIO),
            ("sync.load", System.DECIDALO),
            ("sync.plan", None),
            ("sync.operation", TargetSystem.DECIDALO),
            ("sync.run", None),
        ]
        run_event = _run_event(events)
        assert run_event["level"] == "INFO"
        assert run_event["outcome"] == "success"
        assert run_event["targets"] == ["decidalo"]
        assert "only" not in run_event
        assert run_event["results"] == {"decidalo": {"create": {"success": 1}}}
        assert (run_event["notices"], run_event["errors"], run_event["exit_code"]) == (0, 0, 0)
        # every event belongs to the run
        assert len({event["run_id"] for event in events}) == 1
        assert {(event["environment"], event["dry_run"]) for event in events} == {(Environment.PRODUCTION, False)}
        assert not any(value in str(events) for value in PERSONAL_DATA)

    def test_second_run_writes_nothing(self, events: list[dict[str, object]]) -> None:
        personio, decidalo = _personio(ERIKA, MAX), FakeDecidalo()
        assert _run(personio, decidalo) == ExitCode.SUCCESS
        events.clear()
        assert _run(personio, decidalo) == ExitCode.SUCCESS
        assert len(decidalo.batches) == 1
        (plan_event,) = _named(events, "sync.plan")
        assert plan_event["is_empty"] is True
        assert _named(events, "sync.deviation") == []
        assert _run_event(events)["results"] == {}

    def test_dry_run_writes_nothing(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo()
        options = RunOptions(targets=(TargetSystem.DECIDALO,), dry_run=True)
        assert _run(_personio(ERIKA, MAX), decidalo, options) == ExitCode.SUCCESS
        assert decidalo.batches == []
        assert [(event["employee_number"], event["outcome"]) for event in _named(events, "sync.operation")] == [
            ("00165", Outcome.DRY_RUN),
            ("00200", Outcome.DRY_RUN),
        ]
        run_event = _run_event(events)
        assert run_event["dry_run"] is True
        assert run_event["results"] == {"decidalo": {"create": {"dry_run": 2}}}

    def test_only(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo()
        options = RunOptions(targets=(TargetSystem.DECIDALO,), only=frozenset({"00200", "00999"}))
        assert _run(_personio(ERIKA, MAX), decidalo, options) == ExitCode.SUCCESS
        # all employees are loaded, but only the given ones are planned
        assert decidalo.users[0].employeeID == "00200"
        assert [[user.employeeID for user in batch] for batch in decidalo.batches] == [["00200"]]
        assert [(event["reason"], event["employee_number"]) for event in _named(events, "sync.notice")] == [
            ("unknown_employee_number", "00999")
        ]
        run_event = _run_event(events)
        assert run_event["only"] == ["00200", "00999"]
        assert run_event["notices"] == 1

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_max_employees(self, events: list[dict[str, object]], dry_run: bool) -> None:
        decidalo = FakeDecidalo()
        options = RunOptions(targets=(TargetSystem.DECIDALO,), max_employees=1, dry_run=dry_run)
        assert _run(_personio(ERIKA, MAX), decidalo, options) == ExitCode.SUCCESS
        assert [event["employee_number"] for event in _named(events, "sync.operation")] == ["00165"]
        assert len(decidalo.batches) == (0 if dry_run else 1)
        (plan_event,) = _named(events, "sync.plan")
        assert plan_event["limit_reached"] is True
        assert _run_event(events)["max_employees"] == 1

    def test_notices_and_deviations(self, events: list[dict[str, object]]) -> None:
        without_email = personio_person("3", "00300", email=None)
        renamed = decidalo_user(8, "00165", display_name="Erika Mustermann", entry_date=START)
        decidalo = FakeDecidalo(users=[renamed, decidalo_user(9, None, email="admin@example.com")])
        assert _run(_personio(ERIKA, without_email), decidalo) == ExitCode.SUCCESS
        assert decidalo.batches == []
        assert [(event["level"], event["system"], event["reason"]) for event in _named(events, "sync.notice")] == [
            ("WARNING", "personio", "missing_email"),
            ("WARNING", "decidalo", "missing_employee_number"),
        ]
        assert [
            (event["employee_number"], event["system"], event["fields"]) for event in _named(events, "sync.deviation")
        ] == [("00165", TargetSystem.DECIDALO, ["display_name"])]
        assert _run_event(events)["notices"] == 2
        assert not any(value in str(events) for value in PERSONAL_DATA)

    def test_failed_operation(self, events: list[dict[str, object]]) -> None:
        failed = UserImportResults(items=[import_row("00165", ImportItemStatusType.Failed)])
        decidalo = FakeDecidalo(responses=[failed])
        assert _run(_personio(ERIKA), decidalo) == ExitCode.OPERATION_FAILED
        run_event = _run_event(events)
        assert run_event["level"] == "ERROR"
        assert run_event["outcome"] == "error"
        assert "error" not in run_event
        assert run_event["results"] == {"decidalo": {"create": {"error": 1}}}
        assert (run_event["errors"], run_event["exit_code"]) == (1, 1)

    def test_error_while_loading_aborts_before_planning(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo(fail_get=True)
        assert _run(_personio(ERIKA), decidalo) == ExitCode.ABORTED
        assert decidalo.batches == []
        assert [event["event"] for event in events] == ["sync.load", "sync.load", "sync.run"]
        run_event = _run_event(events)
        assert run_event["level"] == "ERROR"
        assert run_event["error"] == {
            "type": "DecidaloAPIError",
            "status_code": 401,
            "module": "hr_sync.systems.decidalo",
            "function": "_fetch_users",
            "line": ANY,
        }
        assert run_event["exit_code"] == 2

    @pytest.mark.parametrize(
        ("options", "settings", "error"),
        [
            pytest.param(
                RunOptions(),
                _settings,
                {"problem": "target_not_available"},
                id="weclapp",
            ),
            pytest.param(
                DECIDALO,
                lambda: _settings(hr_sync_environment=Environment.TEST),
                {"problem": "test_mode_not_available"},
                id="test mode",
            ),
            pytest.param(
                DECIDALO,
                lambda: _settings(decidalo_api_key=None),
                {"problem": "missing_variables", "variables": ["DECIDALO_API_KEY"]},
                id="missing credential",
            ),
        ],
    )
    def test_configuration_aborts_before_any_system_is_called(
        self,
        events: list[dict[str, object]],
        options: RunOptions,
        settings: Callable[[], Settings],
        error: dict[str, object],
    ) -> None:
        personio, decidalo = _personio(ERIKA), FakeDecidalo()
        assert _run(personio, decidalo, options, settings()) == ExitCode.ABORTED
        assert not personio.opened
        assert decidalo.opened == 0
        run_event = _run_event(events)
        assert run_event["error"] == {
            "type": "ConfigurationError",
            "module": "hr_sync.config",
            "function": "check_settings",
            "line": ANY,
            **error,
        }
        assert run_event["exit_code"] == 2

    def test_unexpected_error_is_logged_with_its_code_location(self, events: list[dict[str, object]]) -> None:
        # a programming error: Decidalo is selected, but has no client
        systems = Systems(personio=_personio(ERIKA))
        assert run(_settings(), DECIDALO, systems=systems, today=TODAY) == ExitCode.ABORTED
        assert _run_event(events)["error"] == {
            "type": "ValueError",
            "module": "hr_sync.sync",
            "function": "_decidalo",
            "line": ANY,
        }

    def test_connects_to_the_systems_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        decidalo = FakeDecidalo()
        connected: list[tuple[TargetSystem, ...]] = []

        def fake_connect(settings: Settings, targets: tuple[TargetSystem, ...]) -> Systems:
            connected.append(targets)
            return Systems(personio=_personio(ERIKA), decidalo=decidalo)

        monkeypatch.setattr("hr_sync.sync.connect", fake_connect)
        assert run(_settings(), DECIDALO) == ExitCode.SUCCESS
        assert connected == [(TargetSystem.DECIDALO,)]
        assert len(decidalo.batches) == 1


class TestConnect:
    """
    Tests of creating the clients from the settings.
    """

    def test_clients(self) -> None:
        systems = connect(_settings(), [TargetSystem.DECIDALO])
        assert isinstance(systems.personio, PersonioClient)
        assert isinstance(systems.decidalo, DecidaloClient)
        # the production system, which can't be configured
        assert systems.decidalo._base_url == "https://import.decidalo.dev"

    def test_only_selected_targets_get_a_client(self) -> None:
        assert connect(_settings(decidalo_api_key=None), []).decidalo is None

    def test_missing_credential(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            connect(_settings(personio_client_id=None), [])
        assert error_info.value.problem == ConfigurationProblem.MISSING_VARIABLES
        assert error_info.value.variables == ("PERSONIO_CLIENT_ID",)


class TestSteps:
    """
    Tests of the steps of a run that a run with Decidalo as the only target doesn't reach.
    """

    def test_create_plan_event(self, events: list[dict[str, object]]) -> None:
        snapshots = load_snapshots(
            Systems(personio=_personio(ERIKA, MAX), decidalo=FakeDecidalo(users=[MAX_IN_DECIDALO])),
            _settings(),
            [TargetSystem.DECIDALO],
            TODAY,
        )
        events.clear()
        create_plan(snapshots)
        (plan_event,) = events
        assert plan_event["event"] == "sync.plan"
        assert plan_event["operations"] == {"decidalo": {"create": 1}}
        assert plan_event["deviations"] == {"decidalo": 0}
        assert plan_event["only_in_target"] == {"decidalo": 0}
        assert (plan_event["notices"], plan_event["is_empty"], plan_event["limit_reached"]) == (0, False, False)

    def test_weclapp_can_not_be_loaded_yet(self) -> None:
        with pytest.raises(NotImplementedError):
            load_snapshots(Systems(personio=_personio(ERIKA)), _settings(), [TargetSystem.WECLAPP], TODAY)

    def test_weclapp_can_only_be_planned_yet(self, events: list[dict[str, object]]) -> None:
        record = EmployeeRecord(employee_number="00165", first_name="Erika")
        operation = Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=record)
        sync_plan = SyncPlan(
            employees=(EmployeePlan(employee_number="00165", operations=(operation,)),),
            only_in_target={TargetSystem.WECLAPP: 0},
        )
        systems = Systems(personio=_personio())
        results = execute_plan(systems, sync_plan, _settings(), dry_run=True)
        assert [result.outcome for result in results] == [Outcome.DRY_RUN]
        with pytest.raises(NotImplementedError):
            execute_plan(systems, sync_plan, _settings(), dry_run=False)

    def test_personio_attribute_from_the_settings(self) -> None:
        settings = _settings(personio_employee_number_attribute=ATTRIBUTE)
        assert set(load_snapshots(Systems(personio=_personio(ERIKA)), settings, [], TODAY).source.records) == {"00165"}
        # Erika has no employee number in another attribute
        settings = _settings(personio_employee_number_attribute="dynamic_0815")
        assert load_snapshots(Systems(personio=_personio(ERIKA)), settings, [], TODAY).source.records == {}
