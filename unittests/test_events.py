import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from hr_sync.events import configure_logging, emit, error_fields, run_context, wide_event

if TYPE_CHECKING:
    from loguru import Message


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


@pytest.fixture
def events() -> Iterator[list[dict[str, object]]]:
    """Collects the events logged during a test."""
    captured: list[dict[str, object]] = []

    def sink(message: "Message") -> None:
        record = message.record
        captured.append({"event": record["message"], "level": record["level"].name, **record["extra"]})

    handler_id = logger.add(sink, level="DEBUG")
    yield captured
    logger.remove(handler_id)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Restores the loguru and standard logging setup that configure_logging replaces."""
    handlers, level = logging.root.handlers[:], logging.root.level
    yield
    logger.remove()
    logger.add(sys.stderr)
    logging.root.handlers[:] = handlers
    logging.root.setLevel(level)


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
        assert lines == [
            {
                "timestamp": lines[0]["timestamp"],
                "level": "WARNING",
                "event": "log",
                "logger": "weclapp_client",
                "message": "GET /user 429, retrying",
            }
        ]

    def test_client_logs_with_custom_level(self) -> None:
        stream = io.StringIO()
        configure_logging("json", stream=stream)
        logging.getLogger("decidalo_client").log(35, "custom level")
        assert json.loads(stream.getvalue())["level"] == "Level 35"
