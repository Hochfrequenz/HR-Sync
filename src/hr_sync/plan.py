"""
The plan: what HR-Sync is going to do per employee and target system.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from hr_sync.model import EmployeeRecord, Notice, TargetSystem


class OperationType(StrEnum):
    """What an operation does in its target system."""

    CREATE = "create"
    """the employee is missing in the target system"""
    COMPLETE = "complete"
    """weclapp only: the user exists, but its employee record is missing"""


class Operation(BaseModel):
    """One change in one target system."""

    model_config = ConfigDict(frozen=True)

    system: TargetSystem
    type: OperationType
    record: EmployeeRecord
    """the desired state from Personio"""


class Deviation(BaseModel):
    """Fields of an existing employee whose values differ from Personio. They are logged, but not changed."""

    model_config = ConfigDict(frozen=True)

    system: TargetSystem
    fields: tuple[str, ...]
    """only the names of the fields, never their values"""


class EmployeePlan(BaseModel):
    """The operations and deviations of one employee."""

    model_config = ConfigDict(frozen=True)

    employee_number: str
    operations: tuple[Operation, ...] = ()
    """in the order of :class:`TargetSystem`"""
    deviations: tuple[Deviation, ...] = ()


class SyncPlan(BaseModel):
    """The plan of a run. It only contains employees with operations or deviations."""

    model_config = ConfigDict(frozen=True)

    employees: tuple[EmployeePlan, ...] = ()
    """sorted by employee number, so that "the first entry" is well-defined"""
    only_in_target: dict[TargetSystem, int]
    """the number of employees per target system that are not loaded from Personio at all"""
    notices: tuple[Notice, ...] = ()
    """hints found while planning, e.g. :attr:`NoticeReason.EMAIL_IN_USE`"""

    @property
    def is_empty(self) -> bool:
        """
        True if there is nothing to execute. Deviations don't count, because they are only logged.
        """
        return not any(employee.operations for employee in self.employees)
