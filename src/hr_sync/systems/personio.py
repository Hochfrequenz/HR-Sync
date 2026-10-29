"""
Loads the employees from Personio, the source of HR-Sync.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from datetime import date
from typing import Literal, Protocol

from personio_client import Employment, Person

from hr_sync.events import wide_event
from hr_sync.model import EmployeeRecord, ExclusionReason, Snapshot, SnapshotBuilder, System, display_name_of

DEFAULT_CONCURRENCY = 5
"""how many persons' employments are requested at the same time; Personio's rate limit bucket held 100 requests"""


class PersonioSource(Protocol):
    """The part of :class:`personio_client.PersonioClient` that the loader uses."""

    def iter_persons(self, *, status: Literal["ACTIVE", "INACTIVE"] | None = None) -> AsyncIterator[Person]: ...

    def iter_employments(self, person_id: str) -> AsyncIterator[Employment]: ...


async def load_personio(
    client: PersonioSource,
    *,
    employee_number_attribute: str,
    today: date,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> Snapshot:
    """
    Loads the persons of Personio and maps those with a relevant employment onto the common model.

    Only the persons with status ``ACTIVE`` are loaded; that includes onboarding persons and persons on leave, whose
    employments :func:`relevant_employment` then sorts out. Persons that are not relevant yield no notice, so future
    employees without employee number or e-mail address don't cause warnings. The employee number is the value of the
    custom attribute with the id ``employee_number_attribute``. ``today`` is the day of the run, i.e. ``date.today()``:
    dates are compared by the day only, without time zones, because all employees are in Germany. Logs one event
    ``sync.load``.
    """
    with wide_event("sync.load", system=System.PERSONIO) as event:
        persons = [person async for person in client.iter_persons(status="ACTIVE")]
        with_id = [(person.id, person) for person in persons if person.id is not None]
        semaphore = asyncio.Semaphore(concurrency)

        async def employments_of(person_id: str) -> list[Employment]:
            async with semaphore:
                return [employment async for employment in client.iter_employments(person_id)]

        all_employments = await asyncio.gather(*(employments_of(person_id) for person_id, _ in with_id))
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        not_relevant: Counter[ExclusionReason] = Counter()
        for (person_id, person), employments in zip(with_id, all_employments, strict=True):
            employee_number = employee_number_of(person, employee_number_attribute)
            employment = relevant_employment(employments, today)
            if isinstance(employment, ExclusionReason):
                builder.exclude(person_id, employee_number, employment)
                if not employment.is_data_problem:
                    not_relevant[employment] += 1
            elif employee_number is None:
                builder.add_without_employee_number(person_id, person.email)
            else:
                builder.add(person_id, _record(person, employee_number, employment))
        snapshot = builder.build()
        event["persons"] = len(persons)
        event["without_id"] = len(persons) - len(with_id)
        event["records"] = len(snapshot.records)
        event["notices"] = len(snapshot.notices)
        event["not_relevant"] = {reason.value: count for reason, count in sorted(not_relevant.items())}
        return snapshot


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


def employee_number_of(person: Person, attribute_id: str) -> str | None:
    """
    Returns the employee number from the custom attribute with the given id, or ``None`` if it is missing or empty.

    The number stays a string, so that leading zeros are kept.
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


def _record(person: Person, employee_number: str, employment: Employment) -> EmployeeRecord:
    return EmployeeRecord(
        employee_number=employee_number,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=display_name_of(person.first_name, person.last_name),
        email=person.email,
        entry_date=employment.employment_start_date,
    )
