import io
import json
import logging
from unittest.mock import ANY

import pytest
from pydantic import ValidationError

from hr_sync.events import configure_logging, emit, emit_operation, error_fields, run_context, wide_event
from hr_sync.model import EmployeeRecord, TargetSystem
from hr_sync.plan import Operation, OperationResult, OperationType, Outcome
from hr_sync.systems.decidalo import execute_decidalo
from unittests.fakes import FakeDecidalo

_OPERATION = Operation(
    system=TargetSystem.DECIDALO,
    type=OperationType.CREATE,
    record=EmployeeRecord(employee_number="00165", first_name="Erika", email="erika.musterfrau@example.com"),
)


class _ApiError(Exception):
    """Mimics the exceptions of the clients: a message with personal data and attributes that are safe to log."""

    def __init__(self, message: str, status_code: int, trace_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.trace_id = trace_id


class _Issue:
    def __init__(self, location: str) -> None:
        self.location = location


class _ValidationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status_code = 400
        self.problem_type = "/webapp/view/api/errors.html#validation"
        self.validation_errors = (_Issue("lastName"), _Issue("email"))


class TestWideEvent:
    """
    Tests of the events that span a unit of work.
    """

    def test_success(self, events: list[dict[str, object]]) -> None:
        with wide_event("sync.load", system="personio") as event:
            event["records"] = 12
        assert len(events) == 1
        logged = events[0]
        assert logged["event"] == "sync.load"
        assert logged["level"] == "INFO"
        assert logged["system"] == "personio"
        assert logged["records"] == 12
        assert logged["outcome"] == "success"
        assert isinstance(logged["duration_ms"], int)
        assert logged["duration_ms"] >= 0

    def test_outcome_set_by_the_caller(self, events: list[dict[str, object]]) -> None:
        with wide_event("sync.operation") as event:
            event["outcome"] = "dry_run"
        assert events[0]["outcome"] == "dry_run"
        assert events[0]["level"] == "INFO"

    def test_outcome_error_set_by_the_caller_is_logged_as_error(self, events: list[dict[str, object]]) -> None:
        with wide_event("sync.operation") as event:
            event["outcome"] = "error"
        assert events[0]["level"] == "ERROR"

    def test_level(self, events: list[dict[str, object]]) -> None:
        with wide_event("sync.plan", level="WARNING"):
            pass
        assert events[0]["level"] == "WARNING"

    def test_exception(self, events: list[dict[str, object]]) -> None:
        with pytest.raises(_ApiError), wide_event("sync.load", system="weclapp"):
            raise _ApiError("Erika Musterfrau", status_code=503)
        assert len(events) == 1
        logged = events[0]
        assert logged["level"] == "ERROR"
        assert logged["outcome"] == "error"
        assert logged["error"] == {"type": "_ApiError", "status_code": 503}
        assert "duration_ms" in logged

    def test_abort_is_not_logged_as_success(self, events: list[dict[str, object]]) -> None:
        with pytest.raises(KeyboardInterrupt), wide_event("sync.run"):
            raise KeyboardInterrupt
        assert events[0]["outcome"] == "error"
        assert events[0]["error"] == {"type": "KeyboardInterrupt"}


class TestEmit:
    """
    Tests of the events that don't span a unit of work.
    """

    def test_emit(self, events: list[dict[str, object]]) -> None:
        emit("sync.notice", level="WARNING", system="personio", reason="missing_email", system_id="42")
        assert events == [
            {
                "event": "sync.notice",
                "level": "WARNING",
                "system": "personio",
                "reason": "missing_email",
                "system_id": "42",
            }
        ]

    def test_run_context(self, events: list[dict[str, object]]) -> None:
        with run_context(run_id="run-1", environment="test", dry_run=True):
            emit("sync.plan")
            with wide_event("sync.load"):
                pass
        emit("outside")
        assert [(event["run_id"], event["dry_run"]) for event in events[:2]] == [("run-1", True), ("run-1", True)]
        assert "run_id" not in events[2]


class TestErrorFields:
    """
    Tests of the fields of an exception that are safe to log.
    """

    def test_message_is_never_part_of_the_fields(self) -> None:
        fields = error_fields(ValueError("Erika Musterfrau <erika.musterfrau@example.com>"))
        assert fields == {"type": "ValueError"}

    def test_personio_error(self) -> None:
        error = _ApiError("not found", status_code=404, trace_id="abc-123")
        assert error_fields(error) == {"type": "_ApiError", "status_code": 404, "trace_id": "abc-123"}

    def test_weclapp_validation_error(self) -> None:
        assert error_fields(_ValidationError("lastName: 'Musterfrau' is too long")) == {
            "type": "_ValidationError",
            "status_code": 400,
            "problem_type": "/webapp/view/api/errors.html#validation",
            "locations": ["lastName", "email"],
        }

    def test_code_location_in_hr_sync(self) -> None:
        with pytest.raises(ValueError, match="batch_size") as error_info:
            execute_decidalo(FakeDecidalo(), [], employee_type_name=None, has_login=None, batch_size=0)
        assert error_fields(error_info.value) == {
            "type": "ValueError",
            "module": "hr_sync.systems.decidalo",
            "function": "execute_decidalo",
            "line": ANY,
        }

    def test_no_code_location_without_a_frame_of_hr_sync(self) -> None:
        # pydantic raises the error, and the validators of HR-Sync have already returned
        with pytest.raises(ValidationError) as error_info:
            EmployeeRecord(employee_number=" ")
        assert error_fields(error_info.value) == {"type": "ValidationError"}

    def test_frames_of_the_logging_are_no_code_location(self, events: list[dict[str, object]]) -> None:
        with pytest.raises(ValueError, match="Erika"), wide_event("sync.load"):
            raise ValueError("Erika")
        assert events[0]["error"] == {"type": "ValueError"}


class TestEmitOperation:
    """
    Tests of the event of an executed or planned operation.
    """

    @pytest.mark.parametrize(
        ("outcome", "level"),
        [
            pytest.param(Outcome.SUCCESS, "INFO", id="success"),
            pytest.param(Outcome.DRY_RUN, "INFO", id="dry run"),
            pytest.param(Outcome.SKIPPED, "WARNING", id="skipped"),
            pytest.param(Outcome.ERROR, "ERROR", id="error"),
        ],
    )
    def test_level(self, events: list[dict[str, object]], outcome: Outcome, level: str) -> None:
        emit_operation(OperationResult(operation=_OPERATION, outcome=outcome))
        assert events == [
            {
                "event": "sync.operation",
                "level": level,
                "employee_number": "00165",
                "system": "decidalo",
                "type": "create",
                "outcome": outcome,
            }
        ]

    def test_optional_fields(self, events: list[dict[str, object]]) -> None:
        result = OperationResult(
            operation=_OPERATION, outcome=Outcome.ERROR, target_id="42", detail="Failed", error={"type": "ValueError"}
        )
        emit_operation(result)
        assert (events[0]["target_id"], events[0]["detail"], events[0]["error"]) == (
            "42",
            "Failed",
            {"type": "ValueError"},
        )
        assert "Erika" not in str(events[0])


@pytest.mark.usefixtures("restore_logging")
class TestConfigureLogging:
    """
    Tests of the output of the events.
    """

    def test_json(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        with wide_event("sync.load", system="personio") as event:
            event["records"] = 3
        line = json.loads(stream.getvalue())
        assert line["event"] == "sync.load"
        assert line["level"] == "INFO"
        assert line["system"] == "personio"
        assert line["records"] == 3
        assert line["outcome"] == "success"
        assert "timestamp" in line

    def test_one_line_per_event(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        emit("first")
        emit("second")
        assert [json.loads(line)["event"] for line in stream.getvalue().splitlines()] == ["first", "second"]

    def test_pretty(self) -> None:
        stream = io.StringIO()
        configure_logging("pretty", stream=stream)
        with pytest.raises(_ApiError), wide_event("sync.load", system="weclapp"):
            raise _ApiError("Erika Musterfrau", status_code=503)
        output = stream.getvalue()
        assert "| ERROR   | sync.load system=weclapp outcome=error error.type=_ApiError error.status_code=503" in output

    def test_defaults_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("json")
        emit("sync.plan")
        assert json.loads(capsys.readouterr().out)["event"] == "sync.plan"

    def test_no_personal_data_from_exceptions(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        secret = "Erika Musterfrau <erika.musterfrau@example.com>"
        with pytest.raises(ValueError, match="Erika"), wide_event("sync.operation", employee_number="00165"):
            raise ValueError(secret)
        output = stream.getvalue()
        assert "Erika" not in output
        assert "erika.musterfrau@example.com" not in output
        assert "Traceback" not in output

    def test_client_logs_from_warning(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        client_logger = logging.getLogger("weclapp_client")
        client_logger.info("GET /user 200")
        client_logger.warning("GET /user 429, retrying")
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert len(lines) == 1
        assert {key: value for key, value in lines[0].items() if key not in ("timestamp", "line")} == {
            "level": "WARNING",
            "event": "log",
            "logger": "weclapp_client",
            "function": "test_client_logs_from_warning",
        }
        assert isinstance(lines[0]["line"], int)

    def test_client_log_messages_are_not_forwarded(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        client_logger = logging.getLogger("personio_client")
        error = ValueError("Erika Musterfrau <erika.musterfrau@example.com>")
        client_logger.warning("API failed: %s", error)
        try:
            raise error
        except ValueError:
            client_logger.exception("API failed for Erika Musterfrau")
        output = stream.getvalue()
        assert "Erika" not in output
        assert "erika.musterfrau@example.com" not in output
        assert "API failed" not in output
        assert [json.loads(line).get("error") for line in output.splitlines()] == [None, {"type": "ValueError"}]

    def test_client_logger_with_a_lower_level_is_filtered_too(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        client_logger = logging.getLogger("decidalo_client.verbose")
        client_logger.setLevel(logging.DEBUG)
        try:
            client_logger.debug("POST /importapi/User/ImportSync")
            client_logger.info("200 OK")
        finally:
            client_logger.setLevel(logging.NOTSET)
        assert stream.getvalue() == ""

    def test_pretty_keeps_one_line_per_event(self) -> None:
        stream = io.StringIO()
        configure_logging("pretty", stream=stream)
        emit("sync.notice", detail="first\r\nsecond")
        output = stream.getvalue()
        assert output.count("\n") == 1
        assert "detail=first\\r\\nsecond" in output

    def test_client_logs_with_custom_level(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        logging.getLogger("decidalo_client").log(35, "custom level")
        assert json.loads(stream.getvalue())["level"] == "Level 35"
