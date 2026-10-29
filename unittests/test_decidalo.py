from datetime import date
from unittest.mock import ANY

import aiohttp
import pytest
from decidalo_client import DecidaloAPIError, DecidaloClient
from decidalo_client.models import (
    ImportItemStatusType,
    UserImportResults,
    UserImportRowResult,
)

from hr_sync.model import EmployeeRecord, Notice, NoticeReason, System, TargetSystem
from hr_sync.plan import Operation, OperationResult, OperationType, Outcome
from hr_sync.systems.decidalo import (
    MAX_BATCH_SIZE,
    DecidaloTarget,
    execute_decidalo,
    fetch_users,
    load_decidalo,
    map_users,
    user_input,
)
from unittests.fakes import SECRET, FakeDecidalo, decidalo_user, import_row

SENT_FROM = {"module": "hr_sync.systems.decidalo", "function": "_send_batches", "line": ANY}
"""the code location of an error that ends a request"""


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


def _execute(client: FakeDecidalo, operations: list[Operation], batch_size: int = 100) -> list[OperationResult]:
    return execute_decidalo(client, operations, employee_type_name="Internal", has_login=True, batch_size=batch_size)


class TestMapUsers:
    """
    Tests of mapping the users of Decidalo onto the common model, with plain data.
    """

    def test_user(self) -> None:
        snapshot = map_users([decidalo_user()])
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
        snapshot = map_users([decidalo_user(43, employee_id, email="admin@example.com")])
        assert snapshot.records == {}
        assert snapshot.notices == [
            Notice(system=System.DECIDALO, reason=NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id="43")
        ]
        # its e-mail address is still in use
        assert snapshot.email_owners == {"admin@example.com": ("43",)}

    def test_deactivated_user_is_part_of_the_snapshot(self) -> None:
        snapshot = map_users([decidalo_user(deactivated=True), decidalo_user(44, "00200", email="max@example.com")])
        assert set(snapshot.records) == {"00165", "00200"}
        assert snapshot.deactivated == frozenset({"00165"})
        # only a problem for a relevant employee, which the planner checks
        assert snapshot.notices == []

    def test_duplicate_employee_number(self) -> None:
        snapshot = map_users([decidalo_user(42), decidalo_user(43, email="other@example.com")])
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
        client = FakeDecidalo(users=[decidalo_user()])
        assert fetch_users(client) == [decidalo_user()]
        assert (client.opened, client.closed) == (1, 1)

    def test_client_is_closed_after_an_error(self) -> None:
        client = FakeDecidalo(fail_get=True)
        with pytest.raises(DecidaloAPIError):
            fetch_users(client)
        assert client.closed == 1


class TestLoadDecidalo:
    """
    Tests of loading and mapping together, with the event ``sync.load``.
    """

    def test_event(self, events: list[dict[str, object]]) -> None:
        client = FakeDecidalo(
            users=[
                decidalo_user(42),
                decidalo_user(43, "00200", email="max@example.com", deactivated=True),
                decidalo_user(44, None),
            ]
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
        client = FakeDecidalo()
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
        client = FakeDecidalo()
        _execute(client, [_create(f"00{i}00") for i in range(1, 6)], batch_size=2)
        assert [len(batch) for batch in client.batches] == [2, 2, 1]

    def test_rows_that_are_not_created_are_errors(self, events: list[dict[str, object]]) -> None:
        response = UserImportResults(
            items=[
                import_row("00100", ImportItemStatusType.Failed),
                import_row("00200", ImportItemStatusType.Updated, 7),
                import_row("00300", ImportItemStatusType.NotModified, 8),
                UserImportRowResult(employeeID="00500", status=None),
            ]
        )
        client = FakeDecidalo(responses=[response])
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
        first = UserImportResults(items=[import_row("00100", ImportItemStatusType.Created, 1)])
        client = FakeDecidalo(responses=[first, error])
        operations = [_create("00100"), _create("00200"), _create("00300")]
        results = _execute(client, operations, batch_size=1)
        assert [(result.outcome, result.error) for result in results] == [
            (Outcome.SUCCESS, None),
            (Outcome.ERROR, fields | SENT_FROM),
            (Outcome.SKIPPED, None),
        ]
        assert len(client.batches) == 2
        assert client.closed == 1
        assert [event["level"] for event in events] == ["INFO", "ERROR", "WARNING"]
        assert all("Erika" not in str(event) for event in events)

    def test_programming_errors_are_not_swallowed(self) -> None:
        client = FakeDecidalo(responses=[RuntimeError("bug")])
        with pytest.raises(RuntimeError):
            _execute(client, [_create()])

    def test_no_operations(self) -> None:
        client = FakeDecidalo()
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
        client = FakeDecidalo()
        with pytest.raises(ValueError, match="CREATE"):
            _execute(client, [operation])
        assert client.batches == []

    @pytest.mark.parametrize("batch_size", [0, -1, MAX_BATCH_SIZE + 1])
    def test_batch_size_is_limited(self, batch_size: int) -> None:
        client = FakeDecidalo()
        with pytest.raises(ValueError, match="batch_size"):
            _execute(client, [_create(f"{i:05}") for i in range(MAX_BATCH_SIZE + 1)], batch_size=batch_size)
        assert client.batches == []

    def test_default_batch_size_is_the_limit(self) -> None:
        client = FakeDecidalo()
        execute_decidalo(
            client,
            [_create(f"{i:05}") for i in range(MAX_BATCH_SIZE + 1)],
            employee_type_name="Internal",
            has_login=True,
        )
        assert [len(batch) for batch in client.batches] == [MAX_BATCH_SIZE, 1]
