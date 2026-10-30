"""
The adapter of Personio, the source of HR-Sync.

Like every adapter, it separates loading from mapping:

- :func:`fetch_persons` only talks to Personio and returns the data as it comes.
- :func:`map_persons` applies the rules and builds the snapshot. It contains no IO, so it can be tested and debugged
  with plain data.
- :func:`load_personio` does both and logs the event ``sync.load``.

The Personio client is async only. :func:`fetch_persons` hides that behind a single ``asyncio.run``; everything else
is synchronous.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from types import TracebackType
from typing import Literal, Protocol

from personio_client import Employment, Person

from hr_sync.events import wide_event
from hr_sync.mappings import CAREER_LEVELS, OFFICES, WECLAPP_NAME_MAX_LENGTH, work_schedule_profile
from hr_sync.model import (
    EmployeeRecord,
    ExclusionReason,
    FieldProblem,
    Snapshot,
    SnapshotBuilder,
    System,
    display_name_of,
    phone_number_key,
)


class PersonioSource(Protocol):
    """The part of :class:`personio_client.PersonioClient` that :func:`fetch_persons` uses."""

    async def __aenter__(self) -> object: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None: ...

    def iter_persons(self, *, status: Literal["ACTIVE", "INACTIVE"] | None = None) -> AsyncIterator[Person]: ...

    def iter_employments(self, person_id: str) -> AsyncIterator[Employment]: ...


@dataclass(frozen=True)
class PersonioAttributes:
    """The ids of the custom attributes of Personio that HR-Sync reads, see :class:`hr_sync.config.Settings`."""

    employee_number: str
    career_level: str
    mobile_phone: str


@dataclass(frozen=True)
class LoadedPerson:
    """A person of Personio with its employments, as loaded."""

    person: Person
    employments: tuple[Employment, ...] = ()


@dataclass(frozen=True)
class MappedPersons:
    """The result of :func:`map_persons`: the snapshot and the numbers for the event ``sync.load``."""

    snapshot: Snapshot
    without_id: int = 0
    """persons without id, which can't be part of the snapshot"""
    not_relevant: dict[ExclusionReason, int] = field(default_factory=dict)
    """the number of persons that are not relevant, per reason"""


def load_personio(client: PersonioSource, *, attributes: PersonioAttributes, today: date) -> Snapshot:
    """
    Loads the persons of Personio and maps those with a relevant employment onto the common model.

    See :func:`fetch_persons` and :func:`map_persons`. Logs one event ``sync.load``.
    """
    with wide_event("sync.load", system=System.PERSONIO) as event:
        persons = fetch_persons(client)
        mapped = map_persons(persons, attributes=attributes, today=today)
        event["persons"] = len(persons)
        event["without_id"] = mapped.without_id
        event["records"] = len(mapped.snapshot.records)
        event["notices"] = len(mapped.snapshot.notices)
        event["not_relevant"] = {reason.value: count for reason, count in sorted(mapped.not_relevant.items())}
        return mapped.snapshot


def fetch_persons(client: PersonioSource) -> list[LoadedPerson]:
    """
    Loads the persons with status ``ACTIVE`` and the employments of each person, one request after the other.

    The status ``ACTIVE`` includes onboarding persons and persons on leave; :func:`map_persons` sorts them out. The
    client is opened and closed within a single ``asyncio.run``, so this must not be called from a running event loop.
    Persons without id (all fields are optional in the API) can't be asked for their employments and come without.
    """
    return asyncio.run(_fetch_persons(client))


async def _fetch_persons(client: PersonioSource) -> list[LoadedPerson]:
    async with client:
        persons = [person async for person in client.iter_persons(status="ACTIVE")]
        loaded: list[LoadedPerson] = []
        for person in persons:
            if person.id is None:
                loaded.append(LoadedPerson(person))
                continue
            employments = [employment async for employment in client.iter_employments(person.id)]
            loaded.append(LoadedPerson(person, tuple(employments)))
        return loaded


def map_persons(persons: Sequence[LoadedPerson], *, attributes: PersonioAttributes, today: date) -> MappedPersons:
    """
    Maps the persons with a relevant employment (see :func:`relevant_employment`) onto the common model, without IO.

    Employee number, career level and phone number are the values of the custom attributes in ``attributes``.
    ``today`` is the day of the run, i.e. ``date.today()``: dates are compared by the day only, without time zones,
    because all employees are in Germany. Persons that are not relevant yield no notice, so future employees without
    employee number or e-mail address don't cause warnings. Persons without id are skipped and counted.

    Every transferred field is mandatory, and its value must be part of the mappings (see :func:`map_person`). A person
    with a problem in a field is not part of the snapshot, but still counts for duplicate employee numbers and e-mail
    addresses.
    """
    builder = SnapshotBuilder(System.PERSONIO, require_email=True)
    not_relevant: Counter[ExclusionReason] = Counter()
    without_id = 0
    for loaded in persons:
        person = loaded.person
        if person.id is None:
            without_id += 1
            continue
        employee_number = attribute_value(person, attributes.employee_number)
        employment = relevant_employment(loaded.employments, today)
        if isinstance(employment, ExclusionReason):
            builder.exclude(person.id, employee_number, employment)
            if not employment.is_data_problem:
                not_relevant[employment] += 1
        elif employee_number is None:
            builder.add_without_employee_number(person.id, person.email)
        else:
            record, problems = map_person(person, employment, attributes=attributes, employee_number=employee_number)
            builder.add(person.id, record, problems=problems)
    return MappedPersons(snapshot=builder.build(), without_id=without_id, not_relevant=dict(not_relevant))


def relevant_employment(employments: Sequence[Employment], today: date) -> Employment | ExclusionReason:
    """
    Returns the employment that makes a person relevant for the sync, or why there is none.

    An employment is relevant if its status is ``ACTIVE`` (compared case-insensitively, because Personio isn't
    consistent in its casing) and it started today or earlier. If several employments are, the one with the latest
    start counts. An active employment without start date is a data problem. It is checked before the start dates,
    so that it can't disappear as "not relevant", but only if no other employment is relevant.
    """
    active = [employment for employment in employments if (employment.status or "").upper() == "ACTIVE"]
    started = [
        (employment.employment_start_date, employment)
        for employment in active
        if employment.employment_start_date is not None and employment.employment_start_date <= today
    ]
    if started:
        return max(started, key=lambda pair: pair[0])[1]
    if any(employment.employment_start_date is None for employment in active):
        return ExclusionReason.MISSING_ENTRY_DATE
    if active:
        return ExclusionReason.START_DATE_IN_FUTURE
    return ExclusionReason.EMPLOYMENT_NOT_ACTIVE


def attribute_value(person: Person, attribute_id: str) -> str | None:
    """
    Returns the value of the custom attribute with the given id, or ``None`` if it is missing or empty.

    A number becomes a string, e.g. an employee number; strings are stripped, so that an employee number keeps its
    leading zeros.
    """
    for attribute in person.custom_attributes or []:
        if attribute.id != attribute_id:
            continue
        value = attribute.value
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    return None


def map_person(
    person: Person, employment: Employment, *, attributes: PersonioAttributes, employee_number: str
) -> tuple[EmployeeRecord, list[FieldProblem]]:
    """
    Maps a relevant person and its relevant employment onto the common model, and returns the problems in its fields.

    Every field is mandatory. First and last name must fit into weclapp (see
    :data:`hr_sync.mappings.WECLAPP_NAME_MAX_LENGTH`), also if weclapp is no target, so that an employee is created
    everywhere or nowhere. Career level, office and weekly hours must be part of the mappings (see
    :mod:`hr_sync.mappings`); the service category and the vacation days are derived from the career level. The phone
    number stays as in Personio, but it must be convertible to the international form (see
    :func:`hr_sync.model.phone_number_key`). The problems are named by the fields of the record, in their order.
    """
    career_level = attribute_value(person, attributes.career_level)
    mobile_phone = attribute_value(person, attributes.mobile_phone)
    office_id = employment.office.id if employment.office is not None else None
    weekly_hours = employment.weekly_working_hours
    level = CAREER_LEVELS.get(career_level) if career_level is not None else None
    office = OFFICES.get(office_id) if office_id is not None else None
    record = EmployeeRecord(
        employee_number=employee_number,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=display_name_of(person.first_name, person.last_name),
        email=person.email,
        entry_date=employment.employment_start_date,
        career_level=career_level,
        mobile_phone=mobile_phone,
        office=office.name if office is not None else None,
        weekly_hours=weekly_hours,
        service_category=level.service_category if level is not None else None,
        vacation_days=level.vacation_days if level is not None else None,
    )
    problems = [problem for name in ("first_name", "last_name") if (problem := _name_problem(record, name))]
    problems.extend(_mapped("career_level", career_level, level))
    if mobile_phone is None:
        problems.append(_missing("mobile_phone"))
    elif phone_number_key(mobile_phone) is None:
        problems.append(FieldProblem(reason=ExclusionReason.INVALID_PHONE_NUMBER, field="mobile_phone"))
    problems.extend(_mapped("office", office_id, office))
    profile = work_schedule_profile(weekly_hours) if weekly_hours is not None else None
    problems.extend(_mapped("weekly_hours", weekly_hours, profile))
    return record, problems


def _name_problem(record: EmployeeRecord, field_name: str) -> FieldProblem | None:
    name: str | None = getattr(record, field_name)
    if name is None:
        return _missing(field_name)
    if len(name) > WECLAPP_NAME_MAX_LENGTH:
        return FieldProblem(reason=ExclusionReason.VALUE_TOO_LONG, field=field_name)
    return None


def _missing(field_name: str) -> FieldProblem:
    return FieldProblem(reason=ExclusionReason.MISSING_FIELD, field=field_name)


def _mapped(field_name: str, value: object, mapped: object) -> list[FieldProblem]:
    """
    Returns the problem of a field whose value must be part of a mapping: ``mapped`` is the value in the mapping.
    """
    if value is None:
        return [_missing(field_name)]
    if mapped is None:
        return [FieldProblem(reason=ExclusionReason.UNMAPPED_VALUE, field=field_name)]
    return []
