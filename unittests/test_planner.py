from datetime import date

import pytest

from hr_sync.model import (
    EmployeeRecord,
    ExclusionReason,
    Notice,
    NoticeReason,
    Snapshot,
    SnapshotBuilder,
    System,
    TargetSystem,
)
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

    @pytest.mark.parametrize("duplicated_in", list(TargetSystem))
    def test_duplicate_employee_number_in_a_target_blocks_the_employee(self, duplicated_in: TargetSystem) -> None:
        builder = SnapshotBuilder(System(duplicated_in.value))
        builder.add("1", ERIKA)
        builder.add("2", ERIKA)
        targets = _targets()
        targets[duplicated_in] = builder.build()
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), targets)
        # nothing happens with the employee, not even in the other target; the target's snapshot names the problem
        assert plan.employees == ()
        assert plan.notices == ()

    @pytest.mark.parametrize("in_use_in", list(TargetSystem))
    def test_email_in_use_blocks_the_employee(self, in_use_in: TargetSystem) -> None:
        builder = SnapshotBuilder(System(in_use_in.value))
        builder.add_without_employee_number("42", "ERIKA.Musterfrau@example.com")
        targets = _targets()
        targets[in_use_in] = builder.build()
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), targets)
        # nothing happens with the employee, not even in the other target
        assert plan.employees == ()
        assert plan.notices == (
            Notice(
                system=System(in_use_in.value),
                reason=NoticeReason.EMAIL_IN_USE,
                system_id="42",
                employee_number="00165",
            ),
        )

    def test_email_of_another_employee_number_is_in_use(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-7", ERIKA.model_copy(update={"employee_number": "00999"}))
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), {TargetSystem.WECLAPP: builder.build()})
        assert plan.employees == ()
        assert plan.is_empty
        assert [(notice.reason, notice.system_id) for notice in plan.notices] == [(NoticeReason.EMAIL_IN_USE, "user-7")]

    def test_own_email_is_not_in_use(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", ERIKA)
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), {TargetSystem.WECLAPP: builder.build()})
        assert plan.employees == ()
        assert plan.notices == ()

    def test_employee_without_email_has_no_email_conflict(self) -> None:
        without_email = ERIKA.model_copy(update={"email": None})
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add_without_employee_number("user-9", "erika.musterfrau@example.com")
        plan = build_plan(_snapshot(System.PERSONIO, without_email), {TargetSystem.WECLAPP: builder.build()})
        assert [operation.type for operation in plan.employees[0].operations] == [OperationType.CREATE]
        assert plan.notices == ()

    def test_email_in_use_blocks_an_employee_that_exists_in_the_target(self) -> None:
        weclapp = SnapshotBuilder(System.WECLAPP)
        weclapp.add("user-1", ERIKA)
        weclapp.add_without_employee_number("user-9", "erika.musterfrau@example.com")
        plan = build_plan(
            _snapshot(System.PERSONIO, ERIKA), _targets(weclapp=weclapp.build(), decidalo=_snapshot(System.DECIDALO))
        )
        assert plan.employees == ()
        assert plan.notices == (
            Notice(
                system=System.WECLAPP,
                reason=NoticeReason.EMAIL_IN_USE,
                system_id="user-9",
                employee_number="00165",
            ),
        )

    def test_a_problem_only_blocks_the_affected_employee(self) -> None:
        weclapp = SnapshotBuilder(System.WECLAPP)
        weclapp.add_without_employee_number("user-9", "erika.musterfrau@example.com")
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA, MAX), _targets(weclapp=weclapp.build()))
        assert plan.employees == (
            EmployeePlan(
                employee_number="00200",
                operations=(
                    Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=MAX),
                    Operation(system=TargetSystem.DECIDALO, type=OperationType.CREATE, record=MAX),
                ),
            ),
        )
        assert [notice.employee_number for notice in plan.notices] == ["00165"]

    def test_blocked_employee_keeps_its_deviations(self) -> None:
        in_weclapp = ERIKA.model_copy(update={"last_name": "Schmidt"})
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", in_weclapp)
        builder.add_without_employee_number("user-9", "erika.musterfrau@example.com")
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), _targets(weclapp=builder.build()))
        assert plan.employees == (
            EmployeePlan(
                employee_number="00165",
                deviations=(Deviation(system=TargetSystem.WECLAPP, fields=("last_name",)),),
            ),
        )
        assert plan.is_empty

    def test_deactivated_user_blocks_the_employee(self) -> None:
        decidalo = SnapshotBuilder(System.DECIDALO)
        decidalo.add("42", ERIKA_IN_DECIDALO, deactivated=True)
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), _targets(decidalo=decidalo.build()))
        # nothing happens with the employee, not even the CREATE in weclapp
        assert plan.employees == ()
        assert plan.notices == (
            Notice(
                system=System.DECIDALO,
                reason=NoticeReason.DEACTIVATED_IN_TARGET,
                system_id="42",
                employee_number="00165",
            ),
        )

    def test_deactivated_user_of_a_former_employee_is_no_problem(self) -> None:
        decidalo = SnapshotBuilder(System.DECIDALO)
        decidalo.add("43", MAX.model_copy(update={"first_name": None, "last_name": None}), deactivated=True)
        plan = build_plan(_snapshot(System.PERSONIO, ERIKA), _targets(decidalo=decidalo.build()))
        assert plan.notices == ()
        assert [operation.system for operation in plan.employees[0].operations] == list(TargetSystem)


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
