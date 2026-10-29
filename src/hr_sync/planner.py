"""
Compares the snapshots and creates the plan. Contains no IO.
"""

from collections.abc import Collection, Mapping

from hr_sync.model import EmployeeRecord, Notice, NoticeReason, Snapshot, TargetSystem, email_key
from hr_sync.plan import Deviation, EmployeePlan, Operation, OperationType, SyncPlan

COMPARED_FIELDS: Mapping[TargetSystem, tuple[str, ...]] = {
    TargetSystem.WECLAPP: ("first_name", "last_name", "email", "entry_date"),
    TargetSystem.DECIDALO: ("display_name", "email", "entry_date"),
}
"""the fields that are compared per target system; Decidalo has no first and last name, only a display name"""

_SET_BY_COMPLETE = frozenset({"entry_date"})
"""the fields that :attr:`OperationType.COMPLETE` sets, because weclapp stores them in the employee record"""


def build_plan(
    source: Snapshot,
    targets: Mapping[TargetSystem, Snapshot],
    *,
    only: Collection[str] | None = None,
) -> SyncPlan:
    """
    Creates the plan for the employees from the source (Personio) and the given target systems.

    Employees missing in a target system are created. Existing employees are only compared; their deviations are part
    of the plan, but not changed. With ``only``, the plan is restricted to these employee numbers.

    If there is a data problem with an employee in any target system, nothing happens with this employee, in no
    target system: an employee is only created if everything about them is consistent. The problems are logged as
    notices: duplicate employee numbers by the target's snapshot; e-mail addresses that belong to another user
    (:attr:`NoticeReason.EMAIL_IN_USE`) and deactivated users (:attr:`NoticeReason.DEACTIVATED_IN_TARGET`) by the
    plan, because they are only a problem for a relevant employee.
    """
    employees: list[EmployeePlan] = []
    notices: list[Notice] = []
    for employee_number in sorted(source.records):
        if only is not None and employee_number not in only:
            continue
        desired = source.records[employee_number]
        operations: list[Operation] = []
        deviations: list[Deviation] = []
        has_problem = False
        for system in TargetSystem:
            if system not in targets:
                continue
            target = targets[system]
            if employee_number in target.excluded:
                # e.g. a duplicate employee number, which the target's notices already name
                has_problem = True
                continue
            if employee_number in target.deactivated:
                # e.g. a re-entry: someone has to reactivate the user, HR-Sync only creates
                notices.append(
                    Notice(
                        system=target.system,
                        reason=NoticeReason.DEACTIVATED_IN_TARGET,
                        system_id=target.system_ids.get(employee_number),
                        employee_number=employee_number,
                    )
                )
                has_problem = True
            conflicts = _email_conflicts(employee_number, desired, target)
            if conflicts:
                # creating the employee would yield a duplicate (weclapp) or change a foreign user (Decidalo)
                notices.extend(conflicts)
                has_problem = True
            existing = target.records.get(employee_number)
            if existing is None:
                operations.append(Operation(system=system, type=OperationType.CREATE, record=desired))
                continue
            fields = COMPARED_FIELDS[system]
            if employee_number in target.incomplete:
                operations.append(Operation(system=system, type=OperationType.COMPLETE, record=desired))
                fields = tuple(field for field in fields if field not in _SET_BY_COMPLETE)
            differing = _differing_fields(desired, existing, fields)
            if differing:
                deviations.append(Deviation(system=system, fields=differing))
        if has_problem:
            operations = []
        if operations or deviations:
            employees.append(
                EmployeePlan(
                    employee_number=employee_number, operations=tuple(operations), deviations=tuple(deviations)
                )
            )
    known = source.records.keys() | source.excluded.keys()
    return SyncPlan(
        employees=tuple(employees),
        only_in_target={system: len(target.records.keys() - known) for system, target in targets.items()},
        notices=tuple(notices),
    )


def check_only(source: Snapshot, only: Collection[str]) -> list[Notice]:
    """
    Returns a notice for every employee number passed with ``--only`` that can't be part of the plan.

    Employees excluded because of a data problem get no further notice: the data problem has its own notice.
    """
    notices: list[Notice] = []
    for employee_number in sorted(only):
        if employee_number in source.records:
            continue
        reason = source.excluded.get(employee_number)
        if reason is None:
            notices.append(
                Notice(
                    system=source.system,
                    reason=NoticeReason.UNKNOWN_EMPLOYEE_NUMBER,
                    employee_number=employee_number,
                )
            )
        elif not reason.is_data_problem:
            notices.append(
                Notice(
                    system=source.system,
                    reason=NoticeReason.NOT_RELEVANT,
                    employee_number=employee_number,
                    detail=reason.value,
                )
            )
    return notices


def _email_conflicts(employee_number: str, desired: EmployeeRecord, target: Snapshot) -> list[Notice]:
    """
    Returns a notice for every user of the target that uses the employee's e-mail address but is not the employee.
    """
    if desired.email is None:
        return []
    own_id = target.system_ids.get(employee_number)
    return [
        Notice(
            system=target.system,
            reason=NoticeReason.EMAIL_IN_USE,
            system_id=owner,
            employee_number=employee_number,
        )
        for owner in target.email_owners.get(email_key(desired.email), ())
        if owner != own_id
    ]


def _differing_fields(desired: EmployeeRecord, existing: EmployeeRecord, fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(field for field in fields if not _same(field, getattr(desired, field), getattr(existing, field)))


def _same(field: str, desired: object, existing: object) -> bool:
    if field == "email" and isinstance(desired, str) and isinstance(existing, str):
        return desired.casefold() == existing.casefold()
    return desired == existing
