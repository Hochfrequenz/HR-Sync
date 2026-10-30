"""
The adapter of Decidalo, a target system of HR-Sync.

Like every adapter, it separates IO from logic:

- loading: :func:`fetch_users` only talks to Decidalo, :func:`map_users` builds the snapshot without IO, and
  :func:`load_decidalo` does both and logs the event ``sync.load``.
- executing: :func:`send_users` only talks to Decidalo, :func:`evaluate_responses` turns the responses into results
  without IO, and :func:`execute_decidalo` does both and logs one event ``sync.operation`` per operation.

The Decidalo client is async only. :func:`fetch_users` and :func:`send_users` hide that behind a single
``asyncio.run`` each; everything else is synchronous. New users are created with the synchronous import endpoint
``POST /importapi/User/ImportSync``, one request per employee, whose response contains the result of the row.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

import aiohttp
from decidalo_client import DecidaloClientError
from decidalo_client.models import ImportItemStatusType, UserBatchInput, UserImportResults, UserInput, UserOverview

from hr_sync.events import emit_operation, error_fields, wide_event
from hr_sync.model import EmployeeRecord, Snapshot, SnapshotBuilder, System, TargetSystem
from hr_sync.plan import Operation, OperationResult, OperationType, Outcome

_REQUEST_ERRORS = (DecidaloClientError, aiohttp.ClientError, TimeoutError)
"""errors of a whole request; after one of them, the remaining operations are skipped"""


class IncompleteUserListError(Exception):
    """Decidalo returned fewer users than it has, e.g. because of an undocumented limit."""


class DecidaloTarget(Protocol):
    """The part of :class:`decidalo_client.DecidaloClient` that the adapter uses."""

    async def __aenter__(self) -> object: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None: ...

    async def get_users(self, *, top: int | None = None, skip: int | None = None) -> list[UserOverview]: ...

    async def import_users_sync(self, batch: UserBatchInput) -> UserImportResults: ...


def load_decidalo(client: DecidaloTarget) -> Snapshot:
    """
    Loads all users of Decidalo and maps them onto the common model. Logs one event ``sync.load``.
    """
    with wide_event("sync.load", system=System.DECIDALO) as event:
        users = fetch_users(client)
        snapshot = map_users(users)
        event["users"] = len(users)
        event["records"] = len(snapshot.records)
        event["deactivated"] = len(snapshot.deactivated)
        event["notices"] = len(snapshot.notices)
        return snapshot


def fetch_users(client: DecidaloTarget) -> list[UserOverview]:
    """
    Loads all users of Decidalo. Without ``top``, the API returns all of them.

    Afterwards, one more request checks that there is no user after the last one. A list that Decidalo cut silently
    would be dangerous: a missing user would be planned as new, and the upsert of ``ImportSync`` would overwrite it.
    The client is opened and closed within a single ``asyncio.run``, so this must not be called from a running event
    loop.

    Raises:
        IncompleteUserListError: if Decidalo has more users than it returned.
    """
    return asyncio.run(_fetch_users(client))


async def _fetch_users(client: DecidaloTarget) -> list[UserOverview]:
    async with client:
        users = await client.get_users()
        if await client.get_users(top=1, skip=len(users)):
            raise IncompleteUserListError(f"Decidalo returned {len(users)} users, but has more")
        return users


def map_users(users: Sequence[UserOverview]) -> Snapshot:
    """
    Maps the users of Decidalo onto the common model, without IO.

    The employee number is the ``employeeID``. Decidalo has no first and last name, only the display name. Deactivated
    users (``deactivationDate`` set) are part of the snapshot, marked in :attr:`Snapshot.deactivated`: they still
    exist, and they are only a problem if their employee is relevant, which the planner checks.
    """
    builder = SnapshotBuilder(System.DECIDALO)
    for user in users:
        system_id = str(user.userID)
        employee_number = (user.employeeID or "").strip()
        if not employee_number:
            builder.add_without_employee_number(system_id, user.email)
            continue
        record = EmployeeRecord(
            employee_number=employee_number,
            display_name=user.displayName,
            email=user.email,
            entry_date=user.entryDate,
        )
        builder.add(system_id, record, deactivated=user.deactivationDate is not None)
    return builder.build()


@dataclass(frozen=True)
class NewUserDefaults:
    """
    The values that every new user gets in Decidalo, besides the data of the employee. ``None`` leaves a field to
    Decidalo.

    Decidalo identifies an employee type and an authorization role by id or by name. For the employee type, the id
    wins if the name doesn't exist: in the test environment, the employee type 2 has another name than in production.
    """

    employee_type_id: int | None = None
    employee_type_name: str | None = None
    authorization_role_id: int | None = None
    authorization_role_name: str | None = None
    location: str | None = None
    country_code: str | None = None
    company_name: str | None = None
    has_login: bool | None = None


def execute_decidalo(
    client: DecidaloTarget, operations: Sequence[Operation], *, defaults: NewUserDefaults
) -> list[OperationResult]:
    """
    Creates the employees of the operations in Decidalo, one after the other: one ``ImportSync`` request per
    employee, with the ``defaults`` for every new user.

    An operation succeeds only if its row has the status ``Created``. Any other status means that the row failed or
    that Decidalo matched an existing user, although HR-Sync only sends new ones. If a request fails as a whole, its
    operation is an error and the remaining operations are skipped; the next run catches up on them. Logs one event
    ``sync.operation`` per operation, without the error messages of Decidalo, which may contain personal data.

    Raises:
        ValueError: if an operation is not a Decidalo ``CREATE``.
    """
    for operation in operations:
        if operation.system != TargetSystem.DECIDALO or operation.type != OperationType.CREATE:
            raise ValueError(f"Decidalo can only execute CREATE operations for Decidalo, got {operation.type}")
    if not operations:
        return []
    users = [user_input(operation.record, defaults) for operation in operations]
    results = evaluate_responses(operations, send_users(client, users))
    for result in results:
        emit_operation(result)
    return results


def user_input(record: EmployeeRecord, defaults: NewUserDefaults) -> UserInput:
    """
    Returns the import row that creates the employee in Decidalo. New users are always part of the resource management.
    """
    return UserInput(
        employeeID=record.employee_number,
        displayName=record.display_name,
        email=record.email,
        entryDate=record.entry_date,
        employeeTypeID=defaults.employee_type_id,
        employeeTypeName=defaults.employee_type_name,
        authorizationRoleID=defaults.authorization_role_id,
        authorizationRoleName=defaults.authorization_role_name,
        location=defaults.location,
        countryCode=defaults.country_code,
        companyName=defaults.company_name,
        includeInResourceManagement=True,
        hasLogin=defaults.has_login,
    )


@dataclass(frozen=True)
class FailedRequest:
    """A request that failed as a whole, e.g. because Decidalo was not reachable."""

    error: Exception


def send_users(client: DecidaloTarget, users: Sequence[UserInput]) -> list[UserImportResults | FailedRequest]:
    """
    Sends the users to ``ImportSync``, one request per user and one after the other, and stops after the first request
    that fails as a whole.

    The result has one entry per request that was sent. The client is opened and closed within a single
    ``asyncio.run``, so this must not be called from a running event loop.
    """
    return asyncio.run(_send_users(client, users))


async def _send_users(client: DecidaloTarget, users: Sequence[UserInput]) -> list[UserImportResults | FailedRequest]:
    responses: list[UserImportResults | FailedRequest] = []
    async with client:
        for user in users:
            try:
                responses.append(await client.import_users_sync(UserBatchInput(users=[user])))
            except _REQUEST_ERRORS as error:
                responses.append(FailedRequest(error))
                break
    return responses


def evaluate_responses(
    operations: Sequence[Operation], responses: Sequence[UserImportResults | FailedRequest]
) -> list[OperationResult]:
    """
    Turns the responses of :func:`send_users` into one result per operation, without IO: the n-th response belongs to
    the n-th operation, and operations without response were skipped.

    The row in a response is found by its ``employeeID``, so that a response for another employee can't count as
    success. The ``rowIndex`` (it starts at 0) isn't needed.
    """
    results: list[OperationResult] = []
    for index, operation in enumerate(operations):
        if index >= len(responses):
            results.append(OperationResult(operation=operation, outcome=Outcome.SKIPPED))
            continue
        response = responses[index]
        if isinstance(response, FailedRequest):
            results.append(
                OperationResult(operation=operation, outcome=Outcome.ERROR, error=error_fields(response.error))
            )
            continue
        number = operation.record.employee_number
        row = next((row for row in response.items or [] if row.employeeID == number), None)
        if row is None:
            results.append(OperationResult(operation=operation, outcome=Outcome.ERROR, detail="no_result"))
            continue
        results.append(
            OperationResult(
                operation=operation,
                outcome=Outcome.SUCCESS if row.status == ImportItemStatusType.Created else Outcome.ERROR,
                target_id=str(row.userID) if row.userID is not None else None,
                detail=row.status.value if row.status is not None else "no_status",
            )
        )
    return results
