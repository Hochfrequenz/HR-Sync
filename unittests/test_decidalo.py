from datetime import UTC, date, datetime
from types import TracebackType

import aiohttp
import pytest
from decidalo_client import DecidaloAPIError, DecidaloClient
from decidalo_client.models import (
    ImportItemStatusType,
    UserBatchInput,
    UserImportResults,
    UserImportRowResult,
    UserInput,
    UserOverview,
)

from hr_sync.model import EmployeeRecord, Notice, NoticeReason, System, TargetSystem
from hr_sync.plan import Operation, OperationResult, OperationType, Outcome
from hr_sync.systems.decidalo import (
    DecidaloTarget,
    execute_decidalo,
    fetch_users,
    load_decidalo,
    map_users,
    user_input,
)

SECRET = "Erika Musterfrau <erika.musterfrau@example.com>"
"""personal data in error messages of Decidalo, which must never be logged"""


def _user(
    user_id: int = 42,
    employee_id: str | None = "00165",
    email: str = "erika.musterfrau@example.com",
    display_name: str = "Erika Musterfrau",
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


def _record(employee_number: str = "00165") -> EmployeeRecord:
    return EmployeeRecord(
        employee_number=employee_number,
        first_name="Erika",
        last_name="Musterfrau",
        display_name="Erika Musterfrau",
        email=f"erika.{employee_number}@example.com",
        entry_date=date(2026, 10, 1),
    )


def _create(employee_number: str = "00165") -> Operation:
    return Operation(system=TargetSystem.DECIDALO, type=OperationType.CREATE, record=_record(employee_number))


def _row(employee_id: str, status: ImportItemStatusType, user_id: int | None = None) -> UserImportRowResult:
    return UserImportRowResult(employeeID=employee_id, status=status, userID=user_id, errorMessage=SECRET)


def _created(batch: UserBatchInput) -> UserImportResults:
    return UserImportResults(
        items=[
            _row(user.employeeID or "", ImportItemStatusType.Created, 1000 + index)
            for index, user in enumerate(batch.users)
        ]
    )


class _FakeDecidalo:
    """An async Decidalo client that records how it is used."""

    def __init__(
        self,
        users: list[UserOverview] | None = None,
        responses: list[UserImportResults | Exception] | None = None,
        fail_get: bool = False,
    ) -> None:
        self._users = users or []
        self._responses = responses or []
        self._fail_get = fail_get
        self.batches: list[list[UserInput]] = []
        self.opened = 0
        self.closed = 0

    async def __aenter__(self) -> "_FakeDecidalo":
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
        return list(self._users)

    async def import_users_sync(self, batch: UserBatchInput) -> UserImportResults:
        self.batches.append(list(batch.users))
        response = self._responses.pop(0) if self._responses else _created(batch)
        if isinstance(response, Exception):
            raise response
        return response


def _execute(client: _FakeDecidalo, operations: list[Operation], batch_size: int = 100) -> list[OperationResult]:
    return execute_decidalo(client, operations, employee_type_name="Internal", has_login=True, batch_size=batch_size)


class TestMapUsers:
    """
    Tests of mapping the users of Decidalo onto the common model, with plain data.
    """

    def test_user(self) -> None:
        snapshot = map_users([_user()])
        assert snapshot.system == System.DECIDALO
        assert snapshot.records == {
            "00165": EmployeeRecord(
                employee_number="00165",
                display_name="Erika Musterfrau",
                email="erika.musterfrau@example.com",
                entry_date=date(2026, 10, 1),
            )
        }
        assert snapshot.system_ids == {"00165": "42"}
        assert snapshot.deactivated == frozenset()
        assert snapshot.notices == []

    @pytest.mark.parametrize("employee_id", [None, "", "  "])
    def test_user_without_employee_number(self, employee_id: str | None) -> None:
        snapshot = map_users([_user(43, employee_id, email="admin@example.com")])
        assert snapshot.records == {}
        assert snapshot.notices == [
            Notice(system=System.DECIDALO, reason=NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id="43")
        ]
        # its e-mail address is still in use
        assert snapshot.email_owners == {"admin@example.com": ("43",)}

    def test_deactivated_user_is_part_of_the_snapshot(self) -> None:
        snapshot = map_users([_user(deactivated=True), _user(44, "00200", email="max@example.com")])
        assert set(snapshot.records) == {"00165", "00200"}
        assert snapshot.deactivated == frozenset({"00165"})
        # only a problem for a relevant employee, which the planner checks
        assert snapshot.notices == []

    def test_duplicate_employee_number(self) -> None:
        snapshot = map_users([_user(42), _user(43, email="other@example.com")])
        assert snapshot.records == {}
        assert [(notice.reason, notice.system_id) for notice in snapshot.notices] == [
            (NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, "42"),
            (NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, "43"),
        ]


class TestFetchUsers:
    """
    Tests of loading the users with the (async) Decidalo client.
    """

    def test_fetch_users(self) -> None:
        client = _FakeDecidalo(users=[_user()])
        assert fetch_users(client) == [_user()]
        assert (client.opened, client.closed) == (1, 1)

    def test_client_is_closed_after_an_error(self) -> None:
        client = _FakeDecidalo(fail_get=True)
        with pytest.raises(DecidaloAPIError):
            fetch_users(client)
        assert client.closed == 1


class TestLoadDecidalo:
    """
    Tests of loading and mapping together, with the event ``sync.load``.
    """

    def test_event(self, events: list[dict[str, object]]) -> None:
        client = _FakeDecidalo(
            users=[_user(42), _user(43, "00200", email="max@example.com", deactivated=True), _user(44, None)]
        )
        snapshot = load_decidalo(client)
        assert set(snapshot.records) == {"00165", "00200"}
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "sync.load"
        assert event["system"] == "decidalo"
        assert event["outcome"] == "success"
        assert (event["users"], event["records"], event["deactivated"], event["notices"]) == (3, 2, 1, 1)

    def test_decidalo_client_is_a_target(self) -> None:
        # mypy checks that the real client fits the protocol the adapter expects
        target: DecidaloTarget = DecidaloClient(api_key="key")
        assert target is not None


class TestUserInput:
    """
    Tests of the import row that creates an employee.
    """

    def test_user_input(self) -> None:
        row = user_input(_record(), employee_type_name="Internal", has_login=True)
        # what the client sends: it drops the None values
        assert row.model_dump(exclude_none=True) == {
            "employeeID": "00165",
            "displayName": "Erika Musterfrau",
            "email": "erika.00165@example.com",
            "entryDate": date(2026, 10, 1),
            "hasLogin": True,
            "employeeTypeName": "Internal",
            "delete": False,
        }

    def test_without_login_and_employee_type(self) -> None:
        row = user_input(_record(), employee_type_name=None, has_login=False)
        assert row.hasLogin is False
        assert row.employeeTypeName is None


class TestExecuteDecidalo:
    """
    Tests of creating employees with ImportSync.
    """

    def test_created(self, events: list[dict[str, object]]) -> None:
        client = _FakeDecidalo()
        operations = [_create("00165"), _create("00200")]
        results = _execute(client, operations)
        assert results == [
            OperationResult(operation=operations[0], outcome=Outcome.SUCCESS, target_id="1000", detail="Created"),
            OperationResult(operation=operations[1], outcome=Outcome.SUCCESS, target_id="1001", detail="Created"),
        ]
        assert [[user.employeeID for user in batch] for batch in client.batches] == [["00165", "00200"]]
        assert (client.opened, client.closed) == (1, 1)
        assert [(event["event"], event["level"], event["employee_number"], event["outcome"]) for event in events] == [
            ("sync.operation", "INFO", "00165", "success"),
            ("sync.operation", "INFO", "00200", "success"),
        ]
        assert events[0]["target_id"] == "1000"
        assert events[0]["type"] == "create"

    def test_batches(self) -> None:
        client = _FakeDecidalo()
        _execute(client, [_create(f"00{i}00") for i in range(1, 6)], batch_size=2)
        assert [len(batch) for batch in client.batches] == [2, 2, 1]

    def test_rows_that_are_not_created_are_errors(self, events: list[dict[str, object]]) -> None:
        response = UserImportResults(
            items=[
                _row("00100", ImportItemStatusType.Failed),
                _row("00200", ImportItemStatusType.Updated, 7),
                _row("00300", ImportItemStatusType.NotModified, 8),
                UserImportRowResult(employeeID="00500", status=None),
            ]
        )
        client = _FakeDecidalo(responses=[response])
        operations = [_create("00100"), _create("00200"), _create("00300"), _create("00400"), _create("00500")]
        results = _execute(client, operations)
        assert [(result.outcome, result.detail) for result in results] == [
            (Outcome.ERROR, "Failed"),
            (Outcome.ERROR, "Updated"),
            (Outcome.ERROR, "NotModified"),
            (Outcome.ERROR, "no_result"),
            (Outcome.ERROR, "no_status"),
        ]
        assert {event["level"] for event in events} == {"ERROR"}
        assert all("Erika" not in str(event) for event in events)

    @pytest.mark.parametrize(
        ("error", "fields"),
        [
            pytest.param(
                DecidaloAPIError(503, SECRET), {"type": "DecidaloAPIError", "status_code": 503}, id="API error"
            ),
            pytest.param(aiohttp.ClientConnectionError(SECRET), {"type": "ClientConnectionError"}, id="connection"),
            pytest.param(TimeoutError(SECRET), {"type": "TimeoutError"}, id="timeout"),
        ],
    )
    def test_failed_request_skips_the_remaining_batches(
        self, events: list[dict[str, object]], error: Exception, fields: dict[str, object]
    ) -> None:
        first = UserImportResults(items=[_row("00100", ImportItemStatusType.Created, 1)])
        client = _FakeDecidalo(responses=[first, error])
        operations = [_create("00100"), _create("00200"), _create("00300")]
        results = _execute(client, operations, batch_size=1)
        assert [(result.outcome, result.error) for result in results] == [
            (Outcome.SUCCESS, None),
            (Outcome.ERROR, fields),
            (Outcome.SKIPPED, None),
        ]
        assert len(client.batches) == 2
        assert client.closed == 1
        assert [event["level"] for event in events] == ["INFO", "ERROR", "WARNING"]
        assert all("Erika" not in str(event) for event in events)

    def test_programming_errors_are_not_swallowed(self) -> None:
        client = _FakeDecidalo(responses=[RuntimeError("bug")])
        with pytest.raises(RuntimeError):
            _execute(client, [_create()])

    def test_no_operations(self) -> None:
        client = _FakeDecidalo()
        assert _execute(client, []) == []
        assert client.opened == 0

    @pytest.mark.parametrize(
        "operation",
        [
            pytest.param(
                Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=_record()), id="weclapp"
            ),
            pytest.param(
                Operation(system=TargetSystem.DECIDALO, type=OperationType.COMPLETE, record=_record()), id="complete"
            ),
        ],
    )
    def test_only_decidalo_creates(self, operation: Operation) -> None:
        client = _FakeDecidalo()
        with pytest.raises(ValueError, match="CREATE"):
            _execute(client, [operation])
        assert client.batches == []

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            _execute(_FakeDecidalo(), [_create()], batch_size=0)
