"""
Fake clients of the systems and builders for their data, shared by the tests of the adapters and of whole runs.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Literal

from decidalo_client import DecidaloAPIError
from decidalo_client.models import (
    ImportItemStatusType,
    UserBatchInput,
    UserImportResults,
    UserImportRowResult,
    UserInput,
    UserOverview,
)
from personio_client import Employment, Person
from personio_client.models import EmploymentOffice, PersonCustomAttribute

from hr_sync.systems.personio import PersonioAttributes

ATTRIBUTE = "company_employee_id"
"""the custom attribute of Personio with the employee number"""
CAREER_LEVEL_ATTRIBUTE = "dynamic_625934eadbe629.22673150"
MOBILE_PHONE_ATTRIBUTE = "dynamic_6239d4b55575b2.63567609"
ATTRIBUTES = PersonioAttributes(
    employee_number=ATTRIBUTE, career_level=CAREER_LEVEL_ATTRIBUTE, mobile_phone=MOBILE_PHONE_ATTRIBUTE
)
"""the custom attributes of Personio, the defaults of the settings"""
GRUENWALD = "906856"
"""the id of the office in Grünwald in Personio"""

SECRET = "Erika Musterfrau <erika.musterfrau@example.com>"
"""personal data in error messages of the systems, which must never be logged"""
PHONE = "0151 27 409 444"
"""the phone number of the test data, as in Personio"""


def personio_employment(
    status: str | None = "ACTIVE",
    start: date | None = date(2026, 9, 1),
    office_id: str | None = GRUENWALD,
    weekly_hours: float | None = 40,
) -> Employment:
    office = EmploymentOffice(id=office_id) if office_id is not None else None
    return Employment(status=status, employment_start_date=start, office=office, weekly_working_hours=weekly_hours)


def personio_person(
    person_id: str | None = "1",
    employee_number: str | None = "00165",
    email: str | None = "erika.musterfrau@example.com",
    first_name: str | None = "Erika",
    last_name: str | None = "Musterfrau",
    career_level: str | None = "Senior Consultant",
    mobile_phone: str | None = PHONE,
) -> Person:
    attributes = [PersonCustomAttribute(id="dynamic_4711", type="string", value="something else")]
    for attribute_id, value in (
        (ATTRIBUTE, employee_number),
        (CAREER_LEVEL_ATTRIBUTE, career_level),
        (MOBILE_PHONE_ATTRIBUTE, mobile_phone),
    ):
        if value is not None:
            attributes.append(PersonCustomAttribute(id=attribute_id, type="string", value=value))
    return Person(id=person_id, first_name=first_name, last_name=last_name, email=email, custom_attributes=attributes)


class FakePersonio:
    """An async Personio client that returns the given persons and employments and records how it is used."""

    def __init__(self, entries: list[tuple[Person, list[Employment]]], fail_for: str | None = None) -> None:
        self._persons = [person for person, _ in entries]
        self._employments = {person.id: employments for person, employments in entries}
        self._fail_for = fail_for
        self.status_filters: list[str | None] = []
        self.requested: list[str] = []
        self.running = 0
        self.max_running = 0
        self.opened = False
        self.closed = False

    async def __aenter__(self) -> "FakePersonio":
        self.opened = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    async def iter_persons(self, *, status: Literal["ACTIVE", "INACTIVE"] | None = None) -> AsyncIterator[Person]:
        self.status_filters.append(status)
        for person in self._persons:
            yield person

    async def iter_employments(self, person_id: str) -> AsyncIterator[Employment]:
        self.requested.append(person_id)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0)
            if person_id == self._fail_for:
                raise ConnectionError("Personio is not reachable")
            for employment in self._employments[person_id]:
                yield employment
        finally:
            self.running -= 1


def decidalo_user(
    user_id: int = 42,
    employee_id: str | None = "00165",
    email: str | None = "erika.musterfrau@example.com",
    display_name: str | None = "Erika Musterfrau",
    entry_date: date | None = date(2026, 10, 1),
    deactivated: bool = False,
) -> UserOverview:
    return UserOverview.model_validate(
        {
            "userID": user_id,
            "employeeID": employee_id,
            "email": email,
            "displayName": display_name,
            "entryDate": entry_date,
            "deactivationDate": datetime(2026, 1, 1, tzinfo=UTC) if deactivated else None,
            "employeeTypeID": 1,
            "employeeTypeName": "Internal",
            "includeInResourceManagement": True,
            "hasLogin": False,
            "creationDate": datetime(2025, 1, 1, tzinfo=UTC),
            "lastEditDate": datetime(2025, 1, 1, tzinfo=UTC),
        }
    )


def import_row(employee_id: str, status: ImportItemStatusType, user_id: int | None = None) -> UserImportRowResult:
    return UserImportRowResult(employeeID=employee_id, status=status, userID=user_id, errorMessage=SECRET)


class FakeDecidalo:
    """
    An async Decidalo client that records how it is used.

    Without a given response, ``ImportSync`` creates the users like Decidalo, with the ids 1000, 1001 and so on, so
    that the next run loads them.
    """

    def __init__(
        self,
        users: list[UserOverview] | None = None,
        responses: list[UserImportResults | Exception] | None = None,
        fail_get: bool = False,
        limit: int | None = None,
    ) -> None:
        self.users = list(users or [])
        self._responses = responses or []
        self._fail_get = fail_get
        self._limit = limit
        self.user_requests: list[tuple[int | None, int | None]] = []
        """``top`` and ``skip`` of every request for users"""
        self.batches: list[list[UserInput]] = []
        self.opened = 0
        self.closed = 0
        self._next_id = 1000

    async def __aenter__(self) -> "FakeDecidalo":
        self.opened += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed += 1

    async def get_users(self, *, top: int | None = None, skip: int | None = None) -> list[UserOverview]:
        """Returns the users ordered by id, like Decidalo; with ``limit``, it cuts every list silently."""
        self.user_requests.append((top, skip))
        if self._fail_get:
            raise DecidaloAPIError(401, "invalid API key")
        users = sorted(self.users, key=lambda user: user.userID or 0)[skip or 0 :]
        if top is not None:
            users = users[:top]
        return users[: self._limit] if self._limit is not None else users

    async def import_users_sync(self, batch: UserBatchInput) -> UserImportResults:
        self.batches.append(list(batch.users))
        response = self._responses.pop(0) if self._responses else self._create(batch)
        if isinstance(response, Exception):
            raise response
        return response

    def _create(self, batch: UserBatchInput) -> UserImportResults:
        rows: list[UserImportRowResult] = []
        for user in batch.users:
            self.users.append(
                decidalo_user(self._next_id, user.employeeID, user.email, user.displayName, user.entryDate)
            )
            rows.append(import_row(user.employeeID or "", ImportItemStatusType.Created, self._next_id))
            self._next_id += 1
        return UserImportResults(items=rows)
