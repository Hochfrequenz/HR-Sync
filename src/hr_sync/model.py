"""
The common data model. Every system is mapped onto it, so that the planner can compare the systems.
"""

from collections import defaultdict
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class System(StrEnum):
    """The systems HR-Sync reads from."""

    PERSONIO = "personio"
    WECLAPP = "weclapp"
    DECIDALO = "decidalo"


class TargetSystem(StrEnum):
    """The systems HR-Sync writes to, in the order in which the plan is executed."""

    WECLAPP = "weclapp"
    DECIDALO = "decidalo"


class EmployeeRecord(BaseModel):
    """
    The master data of an employee in one system.

    Strings are stripped, and empty strings become ``None``, so that the systems can be compared directly.
    """

    model_config = ConfigDict(frozen=True)

    employee_number: str = Field(min_length=1)
    """the key; always a string, so that leading zeros are kept"""
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    """Decidalo only has a display name; for Personio, it's derived with :func:`display_name_of`"""
    email: str | None = None
    entry_date: date | None = None

    @field_validator("employee_number", mode="before")
    @classmethod
    def _strip_employee_number(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("first_name", "last_name", "display_name", "email", mode="before")
    @classmethod
    def _strip_or_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


def display_name_of(first_name: str | None, last_name: str | None) -> str | None:
    """
    Returns the display name used in Decidalo: first and last name, separated by a space.
    """
    parts = [part.strip() for part in (first_name, last_name) if part and part.strip()]
    return " ".join(parts) or None


class NoticeReason(StrEnum):
    """Why HR-Sync logs a notice."""

    MISSING_EMPLOYEE_NUMBER = "missing_employee_number"
    DUPLICATE_EMPLOYEE_NUMBER = "duplicate_employee_number"
    MISSING_EMAIL = "missing_email"
    MISSING_ENTRY_DATE = "missing_entry_date"
    NOT_RELEVANT = "not_relevant"
    """an employee number passed with ``--only`` belongs to an employee that is not relevant"""
    UNKNOWN_EMPLOYEE_NUMBER = "unknown_employee_number"
    """an employee number passed with ``--only`` doesn't belong to any loaded employee"""


class ExclusionReason(StrEnum):
    """Why a loaded employee with employee number is not part of a snapshot."""

    EMPLOYMENT_NOT_ACTIVE = "employment_not_active"
    START_DATE_IN_FUTURE = "start_date_in_future"
    MISSING_ENTRY_DATE = "missing_entry_date"
    MISSING_EMAIL = "missing_email"
    DUPLICATE_EMPLOYEE_NUMBER = "duplicate_employee_number"

    @property
    def is_data_problem(self) -> bool:
        """
        True for data problems, which are logged as notices; False if the employee is just not relevant (yet).
        """
        return self not in (ExclusionReason.EMPLOYMENT_NOT_ACTIVE, ExclusionReason.START_DATE_IN_FUTURE)


class Notice(BaseModel):
    """
    A data problem or a hint, which is logged as a warning.

    It contains no personal data besides the employee number and the id of the record in its system.
    """

    model_config = ConfigDict(frozen=True)

    system: System
    reason: NoticeReason
    system_id: str | None = None
    """the id of the record in its system, which identifies records without employee number"""
    employee_number: str | None = None
    detail: str | None = None


class Snapshot(BaseModel):
    """
    The employees of one system after mapping, without the records that have data problems.
    """

    system: System
    records: dict[str, EmployeeRecord] = Field(default_factory=dict)
    """the valid records by employee number"""
    incomplete: frozenset[str] = frozenset()
    """weclapp only: the employee numbers of users without employee record"""
    excluded: dict[str, ExclusionReason] = Field(default_factory=dict)
    """Personio only: why loaded employees with employee number are not part of :attr:`records`"""
    notices: list[Notice] = Field(default_factory=list)


class SnapshotBuilder:
    """
    Collects the mapped records of one system and sorts out the records with data problems.

    Records without employee number, records with duplicate employee numbers and (if required) records without e-mail
    address are not part of the snapshot. Each of them yields a notice.
    """

    def __init__(self, system: System, *, require_email: bool = False) -> None:
        self._system = system
        self._require_email = require_email
        self._candidates: dict[str, list[tuple[str, EmployeeRecord]]] = defaultdict(list)
        self._incomplete: set[str] = set()
        self._excluded: dict[str, ExclusionReason] = {}
        self._notices: list[Notice] = []

    def add(self, system_id: str, record: EmployeeRecord, *, incomplete: bool = False) -> None:
        """
        Adds a mapped record. Duplicate employee numbers and missing e-mail addresses are checked in :meth:`build`.
        """
        self._candidates[record.employee_number].append((system_id, record))
        if incomplete:
            self._incomplete.add(record.employee_number)

    def add_without_employee_number(self, system_id: str) -> None:
        """
        Notes a record without employee number. It can't be part of the plan, because the key is missing.
        """
        self._notices.append(self._notice(NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id))

    def exclude(self, system_id: str, employee_number: str | None, reason: ExclusionReason) -> None:
        """
        Notes a loaded employee that is not part of the snapshot. Data problems yield a notice.
        """
        if employee_number is not None:
            self._excluded[employee_number] = reason
        if reason.is_data_problem:
            self._notices.append(self._notice(NoticeReason(reason.value), system_id, employee_number))

    def build(self) -> Snapshot:
        """
        Returns the snapshot of the records added so far.
        """
        records: dict[str, EmployeeRecord] = {}
        excluded = dict(self._excluded)
        notices = list(self._notices)
        for employee_number, candidates in self._candidates.items():
            if len(candidates) > 1:
                reason = ExclusionReason.DUPLICATE_EMPLOYEE_NUMBER
            elif self._require_email and candidates[0][1].email is None:
                reason = ExclusionReason.MISSING_EMAIL
            else:
                records[employee_number] = candidates[0][1]
                continue
            excluded[employee_number] = reason
            notices.extend(
                self._notice(NoticeReason(reason.value), system_id, employee_number) for system_id, _ in candidates
            )
        return Snapshot(
            system=self._system,
            records=records,
            incomplete=frozenset(self._incomplete & records.keys()),
            excluded={number: reason for number, reason in excluded.items() if number not in records},
            notices=notices,
        )

    def _notice(self, reason: NoticeReason, system_id: str, employee_number: str | None = None) -> Notice:
        return Notice(system=self._system, reason=reason, system_id=system_id, employee_number=employee_number)
