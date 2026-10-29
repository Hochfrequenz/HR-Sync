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
from personio_client.models import PersonCustomAttribute

ATTRIBUTE = "company_employee_id"
"""the custom attribute of Personio with the employee number"""

SECRET = "Erika Musterfrau <erika.musterfrau@example.com>"
"""personal data in error messages of the systems, which must never be logged"""


def personio_employment(status: str | None = "ACTIVE", start: date | None = date(2026, 9, 1)) -> Employment:
    return Employment(status=status, employment_start_date=start)


def personio_person(
    person_id: str | None = "1",
    employee_number: str | None = "00165",
    email: str | None = "erika.musterfrau@example.com",
    first_name: str = "Erika",
    last_name: str = "Musterfrau",
) -> Person:
    attributes = [PersonCustomAttribute(id="dynamic_4711", type="string", value="something else")]
    if employee_number is not None:
        attributes.append(PersonCustomAttribute(id=ATTRIBUTE, type="string", value=employee_number))
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
    ) -> None:
        self.users = list(users or [])
        self._responses = responses or []
        self._fail_get = fail_get
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

    async def get_users(self) -> list[UserOverview]:
        if self._fail_get:
            raise DecidaloAPIError(401, "invalid API key")
        return list(self.users)

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
