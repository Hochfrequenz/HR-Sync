"""
The adapter of Decidalo, a target system of HR-Sync.

Like every adapter, it separates IO from logic:

- loading: :func:`fetch_users` only talks to Decidalo, :func:`map_users` builds the snapshot without IO, and
  :func:`load_decidalo` does both and logs the event ``sync.load``.
- executing: :func:`send_batches` only talks to Decidalo, :func:`evaluate_batches` turns the responses into results
  without IO, and :func:`execute_decidalo` does both and logs one event ``sync.operation`` per operation.

The Decidalo client is async only. :func:`fetch_users` and :func:`send_batches` hide that behind a single
``asyncio.run`` each; everything else is synchronous. New users are created with the synchronous import endpoint
``POST /importapi/User/ImportSync``, whose response contains the result of every row.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

import aiohttp
from decidalo_client import DecidaloClientError
from decidalo_client.models import ImportItemStatusType, UserBatchInput, UserImportResults, UserInput, UserOverview

from hr_sync.events import emit, error_fields, wide_event
from hr_sync.model import EmployeeRecord, Snapshot, SnapshotBuilder, System, TargetSystem
from hr_sync.plan import Operation, OperationResult, OperationType, Outcome

DEFAULT_BATCH_SIZE = 100
"""the number of users per ImportSync request; the API documents no limit (O7)"""

_REQUEST_ERRORS = (DecidaloClientError, aiohttp.ClientError, TimeoutError)
"""errors of a whole request; after one of them, the remaining batches are skipped"""


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

    async def get_users(self) -> list[UserOverview]: ...

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

    The client is opened and closed within a single ``asyncio.run``, so this must not be called from a running event
    loop.
    """
    return asyncio.run(_fetch_users(client))


async def _fetch_users(client: DecidaloTarget) -> list[UserOverview]:
    async with client:
        return await client.get_users()


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


def execute_decidalo(
    client: DecidaloTarget,
    operations: Sequence[Operation],
    *,
    employee_type_name: str | None,
    has_login: bool | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[OperationResult]:
    """
    Creates the employees of the operations in Decidalo, in batches of at most ``batch_size`` users.

    An operation succeeds only if its row has the status ``Created``. Any other status means that the row failed or
    that Decidalo matched an existing user, although HR-Sync only sends new ones. If a whole request fails, the
    operations of its batch are errors and the remaining batches are skipped; the next run catches up on them. Logs
    one event ``sync.operation`` per operation, without the error messages of Decidalo, which may contain personal
    data.

    Raises:
        ValueError: if ``batch_size`` is not positive or an operation is not a Decidalo ``CREATE``.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    for operation in operations:
        if operation.system != TargetSystem.DECIDALO or operation.type != OperationType.CREATE:
            raise ValueError(f"Decidalo can only execute CREATE operations for Decidalo, got {operation.type}")
    if not operations:
        return []
    batches = [operations[start : start + batch_size] for start in range(0, len(operations), batch_size)]
    users = [
        [
            user_input(operation.record, employee_type_name=employee_type_name, has_login=has_login)
            for operation in batch
        ]
        for batch in batches
    ]
    results = evaluate_batches(batches, send_batches(client, users))
    for result in results:
        _emit_operation(result)
    return results


def user_input(record: EmployeeRecord, *, employee_type_name: str | None, has_login: bool | None) -> UserInput:
    """
    Returns the import row that creates the employee in Decidalo.
    """
    return UserInput(
        employeeID=record.employee_number,
        displayName=record.display_name,
        email=record.email,
        entryDate=record.entry_date,
        hasLogin=has_login,
        employeeTypeName=employee_type_name,
    )


@dataclass(frozen=True)
class FailedRequest:
    """A request that failed as a whole, e.g. because Decidalo was not reachable."""

    error: Exception


def send_batches(
    client: DecidaloTarget, batches: Sequence[Sequence[UserInput]]
) -> list[UserImportResults | FailedRequest]:
    """
    Sends the batches to ``ImportSync``, one after the other, and stops after the first request that fails as a whole.

    The result has one entry per batch that was sent. The client is opened and closed within a single ``asyncio.run``,
    so this must not be called from a running event loop.
    """
    return asyncio.run(_send_batches(client, batches))


async def _send_batches(
    client: DecidaloTarget, batches: Sequence[Sequence[UserInput]]
) -> list[UserImportResults | FailedRequest]:
    responses: list[UserImportResults | FailedRequest] = []
    async with client:
        for batch in batches:
            try:
                responses.append(await client.import_users_sync(UserBatchInput(users=list(batch))))
            except _REQUEST_ERRORS as error:
                responses.append(FailedRequest(error))
                break
    return responses


def evaluate_batches(
    batches: Sequence[Sequence[Operation]], responses: Sequence[UserImportResults | FailedRequest]
) -> list[OperationResult]:
    """
    Turns the responses of :func:`send_batches` into one result per operation, without IO.

    The rows are matched by their ``employeeID``, because the API doesn't document whether ``rowIndex`` starts at 0
    or 1. Batches without response were skipped.
    """
    results: list[OperationResult] = []
    for index, batch in enumerate(batches):
        if index >= len(responses):
            results.extend(OperationResult(operation=operation, outcome=Outcome.SKIPPED) for operation in batch)
            continue
        response = responses[index]
        if isinstance(response, FailedRequest):
            fields = error_fields(response.error)
            results.extend(
                OperationResult(operation=operation, outcome=Outcome.ERROR, error=fields) for operation in batch
            )
            continue
        rows = {row.employeeID: row for row in response.items or [] if row.employeeID is not None}
        for operation in batch:
            row = rows.get(operation.record.employee_number)
            if row is None:
                results.append(OperationResult(operation=operation, outcome=Outcome.ERROR, detail="no_result"))
                continue
            status = row.status.value if row.status is not None else "no_status"
            results.append(
                OperationResult(
                    operation=operation,
                    outcome=Outcome.SUCCESS if row.status == ImportItemStatusType.Created else Outcome.ERROR,
                    target_id=str(row.userID) if row.userID is not None else None,
                    detail=status,
                )
            )
    return results


def _emit_operation(result: OperationResult) -> None:
    levels = {Outcome.ERROR: "ERROR", Outcome.SKIPPED: "WARNING"}
    fields: dict[str, object] = {
        "employee_number": result.operation.record.employee_number,
        "system": result.operation.system,
        "type": result.operation.type,
        "outcome": result.outcome,
    }
    if result.target_id is not None:
        fields["target_id"] = result.target_id
    if result.detail is not None:
        fields["detail"] = result.detail
    if result.error is not None:
        fields["error"] = result.error
    emit("sync.operation", level=levels.get(result.outcome, "INFO"), **fields)
