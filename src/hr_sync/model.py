"""
The common data model. Every system is mapped onto it, so that the planner can compare the systems.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Literal, NamedTuple

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
    career_level: str | None = None
    """e.g. ``Senior Consultant``, see :data:`hr_sync.mappings.CAREER_LEVELS`"""
    mobile_phone: str | None = None
    """the mobile phone number as in Personio; numbers are compared by :func:`phone_number_key`"""
    office: str | None = None
    """the name of the office, e.g. ``Grünwald``, see :data:`hr_sync.mappings.OFFICES`"""
    weekly_hours: float | None = None
    service_category: str | None = None
    """the service category in Decidalo; for Personio, it's derived from the career level"""
    vacation_days: int | None = None
    """the vacation entitlement in days per year in weclapp; for Personio, it's derived from the career level"""

    @field_validator("employee_number", mode="before")
    @classmethod
    def _strip_employee_number(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "first_name",
        "last_name",
        "display_name",
        "email",
        "career_level",
        "mobile_phone",
        "office",
        "service_category",
        mode="before",
    )
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
    EMAIL_IN_USE = "email_in_use"
    """the e-mail address of an employee belongs to another user of the target system"""
    DUPLICATE_EMAIL = "duplicate_email"
    """several employees of the source share an e-mail address"""
    DEACTIVATED_IN_TARGET = "deactivated_in_target"
    """a relevant employee is deactivated in the target system, e.g. after a re-entry"""
    MISSING_FIELD = "missing_field"
    """a mandatory field is missing; the detail is the name of the field"""
    UNMAPPED_VALUE = "unmapped_value"
    """the value of a field is not part of the mappings (see :mod:`hr_sync.mappings`); the detail is the field"""
    INVALID_PHONE_NUMBER = "invalid_phone_number"
    """the phone number can't be converted to the international form (see :func:`phone_number_key`)"""
    VALUE_TOO_LONG = "value_too_long"
    """the value of a field is longer than a target system allows, e.g. a name in weclapp; the detail is the field"""


class ExclusionReason(StrEnum):
    """Why a loaded employee with employee number is not part of a snapshot."""

    EMPLOYMENT_NOT_ACTIVE = "employment_not_active"
    START_DATE_IN_FUTURE = "start_date_in_future"
    MISSING_ENTRY_DATE = "missing_entry_date"
    MISSING_EMAIL = "missing_email"
    DUPLICATE_EMAIL = "duplicate_email"
    DUPLICATE_EMPLOYEE_NUMBER = "duplicate_employee_number"
    MISSING_FIELD = "missing_field"
    UNMAPPED_VALUE = "unmapped_value"
    INVALID_PHONE_NUMBER = "invalid_phone_number"
    VALUE_TOO_LONG = "value_too_long"

    @property
    def is_data_problem(self) -> bool:
        """
        True for data problems, which are logged as notices; False if the employee is just not relevant (yet).
        """
        return self not in (ExclusionReason.EMPLOYMENT_NOT_ACTIVE, ExclusionReason.START_DATE_IN_FUTURE)


class FieldProblem(BaseModel):
    """A data problem with one field of a record, e.g. a missing career level."""

    model_config = ConfigDict(frozen=True)

    reason: Literal[
        ExclusionReason.MISSING_FIELD,
        ExclusionReason.UNMAPPED_VALUE,
        ExclusionReason.INVALID_PHONE_NUMBER,
        ExclusionReason.VALUE_TOO_LONG,
    ]
    field: str
    """the name of the field in :class:`EmployeeRecord`, e.g. ``career_level``"""


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
    system_ids: dict[str, str] = Field(default_factory=dict)
    """the ids of the valid records in their system by employee number"""
    incomplete: frozenset[str] = frozenset()
    """weclapp only: the employee numbers of users without employee record"""
    deactivated: frozenset[str] = frozenset()
    """
    the employee numbers of deactivated users (Decidalo: with deactivation date, weclapp: not active); they are a data
    problem only if the employee is relevant, which the planner checks
    """
    excluded: dict[str, ExclusionReason] = Field(default_factory=dict)
    """
    why loaded employees with employee number are not part of :attr:`records`; in the target systems, these are
    duplicate employee numbers, which the planner must not treat as missing
    """
    email_owners: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """
    the ids of all loaded records that use an e-mail address, by :func:`email_key`, including the records with data
    problems; the planner uses it to find e-mail addresses that belong to another user
    """
    notices: list[Notice] = Field(default_factory=list)


def email_key(email: str) -> str:
    """
    Returns the e-mail address in the form in which addresses are compared: stripped and case-insensitive.
    """
    return email.strip().casefold()


_PHONE_NUMBER = re.compile(r"\+?[0-9 ./-]+")
"""digits, spaces and the separators ``.``, ``/`` and ``-``, with ``+`` only at the start"""
_PHONE_SEPARATORS = re.compile(r"[ ./-]")
_INTERNATIONAL_PHONE_NUMBER = re.compile(r"\+[1-9][0-9]{6,14}")
"""7 to 15 digits, the first of them not 0, like in E.164"""


def phone_number_key(number: str) -> str | None:
    """
    Returns the phone number in the form in which numbers are compared: international, with ``+`` and only digits,
    e.g. ``+4915127409444`` for ``0151 27 409 444``. Returns ``None`` if the number can't be converted.

    A number with ``+`` keeps its country code, ``00`` before the country code becomes ``+``, and a single leading
    ``0`` (a national number) becomes ``+49``, because all employees are in Germany. Besides digits, only spaces and
    the separators ``.``, ``/`` and ``-`` are allowed. Parentheses are not: ``+49 (0) 151 …`` would otherwise become
    the wrong number ``+490151…``.
    """
    number = number.strip()
    if not _PHONE_NUMBER.fullmatch(number):
        return None
    digits = _PHONE_SEPARATORS.sub("", number)
    if digits.startswith("+"):
        key = digits
    elif digits.startswith("00"):
        key = "+" + digits[2:]
    elif digits.startswith("0"):
        key = "+49" + digits[1:]
    else:
        return None
    return key if _INTERNATIONAL_PHONE_NUMBER.fullmatch(key) else None


class _Candidate(NamedTuple):
    system_id: str
    record: EmployeeRecord
    problems: tuple[FieldProblem, ...]


class SnapshotBuilder:
    """
    Collects the mapped records of one system and sorts out the records with data problems.

    Records without employee number and records with duplicate employee numbers are not part of the snapshot. With
    ``require_email``, every employee needs an e-mail address of its own: records without e-mail address and records
    whose address another loaded record uses as well are not part of the snapshot either. Records with problems in
    their fields aren't either. Each of them yields a notice.
    """

    def __init__(self, system: System, *, require_email: bool = False) -> None:
        self._system = system
        self._require_email = require_email
        self._candidates: dict[str, list[_Candidate]] = defaultdict(list)
        self._incomplete: set[str] = set()
        self._deactivated: set[str] = set()
        self._excluded: dict[str, ExclusionReason] = {}
        self._email_owners: dict[str, list[str]] = defaultdict(list)
        self._notices: list[Notice] = []

    def add(
        self,
        system_id: str,
        record: EmployeeRecord,
        *,
        incomplete: bool = False,
        deactivated: bool = False,
        problems: Sequence[FieldProblem] = (),
    ) -> None:
        """
        Adds a mapped record. Duplicate employee numbers and e-mail addresses are checked in :meth:`build`.

        A record with ``problems`` in its fields is not part of the snapshot, but it still counts for the duplicates:
        otherwise, another employee with the same employee number or e-mail address could be created.
        """
        self._candidates[record.employee_number].append(_Candidate(system_id, record, tuple(problems)))
        if incomplete:
            self._incomplete.add(record.employee_number)
        if deactivated:
            self._deactivated.add(record.employee_number)
        self._note_email(system_id, record.email)

    def add_without_employee_number(self, system_id: str, email: str | None = None) -> None:
        """
        Notes a record without employee number. It can't be part of the plan, because the key is missing, but its
        e-mail address is still in use.
        """
        self._notices.append(self._notice(NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id))
        self._note_email(system_id, email)

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

        A duplicate employee number, a missing and a duplicate e-mail address yield only their own notice. Otherwise,
        there is one notice per problem in the fields, so that all of them can be fixed at once.
        """
        records: dict[str, EmployeeRecord] = {}
        system_ids: dict[str, str] = {}
        excluded = dict(self._excluded)
        notices = list(self._notices)
        for employee_number, candidates in self._candidates.items():
            first_id, record, problems = candidates[0]
            if len(candidates) > 1:
                reason = ExclusionReason.DUPLICATE_EMPLOYEE_NUMBER
            elif self._require_email and record.email is None:
                reason = ExclusionReason.MISSING_EMAIL
            elif self._require_email and self._shares_email(record):
                reason = ExclusionReason.DUPLICATE_EMAIL
            elif problems:
                excluded[employee_number] = problems[0].reason
                notices.extend(
                    self._notice(NoticeReason(problem.reason.value), first_id, employee_number, problem.field)
                    for problem in problems
                )
                continue
            else:
                records[employee_number] = record
                system_ids[employee_number] = first_id
                continue
            excluded[employee_number] = reason
            notices.extend(
                self._notice(NoticeReason(reason.value), candidate.system_id, employee_number)
                for candidate in candidates
            )
        return Snapshot(
            system=self._system,
            records=records,
            system_ids=system_ids,
            incomplete=frozenset(self._incomplete & records.keys()),
            deactivated=frozenset(self._deactivated & records.keys()),
            excluded={number: reason for number, reason in excluded.items() if number not in records},
            email_owners={email: tuple(owners) for email, owners in self._email_owners.items()},
            notices=notices,
        )

    def _note_email(self, system_id: str, email: str | None) -> None:
        if email and email.strip():
            self._email_owners[email_key(email)].append(system_id)

    def _shares_email(self, record: EmployeeRecord) -> bool:
        return record.email is not None and len(self._email_owners.get(email_key(record.email), [])) > 1

    def _notice(
        self, reason: NoticeReason, system_id: str, employee_number: str | None = None, detail: str | None = None
    ) -> Notice:
        return Notice(
            system=self._system, reason=reason, system_id=system_id, employee_number=employee_number, detail=detail
        )
