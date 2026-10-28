from datetime import date

from hr_sync.model import EmployeeRecord, ExclusionReason, Notice, NoticeReason, Snapshot, System, TargetSystem
from hr_sync.plan import Deviation, EmployeePlan, Operation, OperationType, SyncPlan
from hr_sync.planner import build_plan, check_only

ERIKA = EmployeeRecord(
    employee_number="00165",
    first_name="Erika",
    last_name="Musterfrau",
    display_name="Erika Musterfrau",
    email="erika.musterfrau@example.com",
    entry_date=date(2026, 10, 1),
)
MAX = EmployeeRecord(
    employee_number="00200",
    first_name="Max",
    last_name="Mustermann",
    display_name="Max Mustermann",
    email="max.mustermann@example.com",
    entry_date=date(2026, 9, 1),
)
ERIKA_IN_DECIDALO = EmployeeRecord(
    employee_number="00165",
    display_name="Erika Musterfrau",
    email="erika.musterfrau@example.com",
    entry_date=date(2026, 10, 1),
)


def _snapshot(
    system: System,
    *records: EmployeeRecord,
    incomplete: frozenset[str] = frozenset(),
    excluded: dict[str, ExclusionReason] | None = None,
) -> Snapshot:
    return Snapshot(
        system=system,
        records={record.employee_number: record for record in records},
        incomplete=incomplete,
        excluded=excluded or {},
    )


def _targets(weclapp: Snapshot | None = None, decidalo: Snapshot | None = None) -> dict[TargetSystem, Snapshot]:
    return {
        TargetSystem.WECLAPP: weclapp or _snapshot(System.WECLAPP),
        TargetSystem.DECIDALO: decidalo or _snapshot(System.DECIDALO),
    }


class TestBuildPlan:
    """
    Tests of comparing the snapshots.
    """

    def test_missing_employee_is_created_in_every_target(self) -> None:
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), _targets())
        assert plan.employees == (
            EmployeePlan(
                employee_number="00165",
                operations=(
                    Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=ERIKA),
                    Operation(system=TargetSystem.DECIDALO, type=OperationType.CREATE, record=ERIKA),
                ),
            ),
        )
        assert not plan.is_empty

    def test_existing_employee_without_deviations_is_not_part_of_the_plan(self) -> None:
        plan = build_plan(
            _snapshot(System.PERSONIO, ERIKA),
            _targets(_snapshot(System.WECLAPP, ERIKA), _snapshot(System.DECIDALO, ERIKA_IN_DECIDALO)),
        )
        assert plan == SyncPlan(only_in_target={TargetSystem.WECLAPP: 0, TargetSystem.DECIDALO: 0})
        assert plan.is_empty

    def test_deviations_are_only_noted(self) -> None:
        in_weclapp = ERIKA.model_copy(update={"last_name": "Schmidt", "entry_date": date(2026, 11, 1)})
        in_decidalo = ERIKA_IN_DECIDALO.model_copy(update={"display_name": "Erika Schmidt"})
        plan = build_plan(
            _snapshot(System.PERSONIO, ERIKA),
            _targets(_snapshot(System.WECLAPP, in_weclapp), _snapshot(System.DECIDALO, in_decidalo)),
        )
        assert plan.employees == (
            EmployeePlan(
                employee_number="00165",
                deviations=(
                    Deviation(system=TargetSystem.WECLAPP, fields=("last_name", "entry_date")),
                    Deviation(system=TargetSystem.DECIDALO, fields=("display_name",)),
                ),
            ),
        )
        assert plan.is_empty

    def test_email_is_compared_case_insensitively(self) -> None:
        in_weclapp = ERIKA.model_copy(update={"email": "Erika.Musterfrau@Example.com"})
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), _targets(weclapp=_snapshot(System.WECLAPP, in_weclapp)))
        assert all(employee.deviations == () for employee in plan.employees)

    def test_incomplete_weclapp_employee_is_completed(self) -> None:
        in_weclapp = ERIKA.model_copy(update={"entry_date": None, "first_name": "Eri"})
        plan = build_plan(
            _snapshot(System.PERSONIO, ERIKA),
            {TargetSystem.WECLAPP: _snapshot(System.WECLAPP, in_weclapp, incomplete=frozenset({"00165"}))},
        )
        assert plan.employees == (
            EmployeePlan(
                employee_number="00165",
                operations=(Operation(system=TargetSystem.WECLAPP, type=OperationType.COMPLETE, record=ERIKA),),
                deviations=(Deviation(system=TargetSystem.WECLAPP, fields=("first_name",)),),
            ),
        )

    def test_only_the_given_targets_are_planned(self) -> None:
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), {TargetSystem.DECIDALO: _snapshot(System.DECIDALO)})
        assert [operation.system for operation in plan.employees[0].operations] == [TargetSystem.DECIDALO]
        assert plan.only_in_target == {TargetSystem.DECIDALO: 0}

    def test_only_restricts_the_plan_to_the_given_employee_numbers(self) -> None:
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA, MAX), _targets(), only={"00200"})
        assert [employee.employee_number for employee in plan.employees] == ["00200"]

    def test_employees_are_sorted_by_employee_number(self) -> None:
        plan = build_plan(_snapshot(System.PERSONIO, MAX, ERIKA), _targets())
        assert [employee.employee_number for employee in plan.employees] == ["00165", "00200"]

    def test_only_in_target_ignores_employees_excluded_in_the_source(self) -> None:
        unknown = MAX.model_copy(update={"employee_number": "00999"})
        excluded = MAX.model_copy(update={"employee_number": "00777"})
        plan = build_plan(
            _snapshot(System.PERSONIO, ERIKA, excluded={"00777": ExclusionReason.EMPLOYMENT_NOT_ACTIVE}),
            _targets(weclapp=_snapshot(System.WECLAPP, ERIKA, unknown, excluded)),
        )
        assert plan.only_in_target == {TargetSystem.WECLAPP: 1, TargetSystem.DECIDALO: 0}


class TestCheckOnly:
    """
    Tests of the notices for employee numbers passed with ``--only``.
    """

    def test_notices(self) -> None:
        source = _snapshot(
            System.PERSONIO,
            ERIKA,
            excluded={
                "00300": ExclusionReason.START_DATE_IN_FUTURE,
                "00400": ExclusionReason.MISSING_EMAIL,
            },
        )
        notices = check_only(source, ["00999", "00400", "00300", "00165"])
        assert notices == [
            Notice(
                system=System.PERSONIO,
                reason=NoticeReason.NOT_RELEVANT,
                employee_number="00300",
                detail="start_date_in_future",
            ),
            Notice(system=System.PERSONIO, reason=NoticeReason.UNKNOWN_EMPLOYEE_NUMBER, employee_number="00999"),
        ]

    def test_no_notices_for_planned_employees(self) -> None:
        assert check_only(_snapshot(System.PERSONIO, ERIKA), ["00165"]) == []
