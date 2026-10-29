"""
Logging as wide events: one event with the whole context per unit of work, instead of many single log lines.

The events must not contain personal data: only employee numbers, ids of the systems, field names and numbers.
That's why errors are logged with :func:`error_fields`, never with their message or traceback.
"""

import json
import logging
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, TextIO

from loguru import logger

if TYPE_CHECKING:
    from loguru import Message, Record

LogFormat = Literal["pretty", "json"]

_ERROR_ATTRIBUTES = ("status_code", "trace_id", "problem_type")
"""attributes of the client exceptions that are safe to log (HTTP status, Personio trace id, weclapp problem type)"""

CLIENT_LOG_LEVEL = logging.WARNING
"""the level from which the standard logging of the client libraries is passed on"""


def configure_logging(log_format: LogFormat = "pretty", *, stream: TextIO | None = None) -> None:
    """
    Writes the events to ``stream`` (default: stdout), one line per event, and passes the standard logging of the
    client libraries on to the events from :data:`CLIENT_LOG_LEVEL`.
    """
    target = stream if stream is not None else sys.stdout
    render = _render_json if log_format == "json" else _render_pretty
    logger.remove()
    # no backtrace and no diagnose: tracebacks could show variable values, i.e. personal data
    logger.add(_sink(target, render), level="DEBUG", backtrace=False, diagnose=False)
    logging.basicConfig(handlers=[_InterceptHandler()], level=CLIENT_LOG_LEVEL, force=True)


@contextmanager
def wide_event(name: str, *, level: str | int = "INFO", **fields: object) -> Iterator[dict[str, object]]:
    """
    Collects the fields of one unit of work and logs them as one event when the unit ends.

    The caller adds fields to the yielded dict. When the block ends, the event gets ``duration_ms`` and ``outcome``
    (``success``, unless the caller set another one). If the block raises an exception, the outcome is ``error``, the
    event gets the :func:`error_fields` of the exception, and the exception is re-raised. Events with the outcome
    ``error`` are logged as ERROR, all others with ``level``.
    """
    event: dict[str, object] = dict(fields)
    start = time.perf_counter()
    try:
        yield event
    except BaseException as error:  # also an abort (e.g. Ctrl+C) must not be logged as success
        event["outcome"] = "error"
        event["error"] = error_fields(error)
        raise
    finally:
        event.setdefault("outcome", "success")
        event["duration_ms"] = round((time.perf_counter() - start) * 1000)
        emit(name, level="ERROR" if event["outcome"] == "error" else level, **event)


def emit(name: str, *, level: str | int = "INFO", **fields: object) -> None:
    """
    Logs an event that doesn't span a unit of work, e.g. a notice about a data problem.
    """
    logger.bind(**fields).log(level, name)


@contextmanager
def run_context(**fields: object) -> Iterator[None]:
    """
    Adds the fields (e.g. ``run_id``, ``environment``, ``dry_run``) to every event logged inside the block.
    """
    with logger.contextualize(**fields):
        yield


def error_fields(error: BaseException) -> dict[str, object]:
    """
    Returns the fields of an exception that are safe to log.

    The message of an exception may contain personal data (e.g. an API error that repeats a name), so it is never
    logged. Only the type and the attributes in :data:`_ERROR_ATTRIBUTES` are, plus the names of the properties that
    a weclapp validation error refers to.
    """
    fields: dict[str, object] = {"type": type(error).__name__}
    for attribute in _ERROR_ATTRIBUTES:
        value = getattr(error, attribute, None)
        if isinstance(value, str | int) and not isinstance(value, bool):
            fields[attribute] = value
    locations = [
        location
        for issue in getattr(error, "validation_errors", ())
        if isinstance(location := getattr(issue, "location", None), str)
    ]
    if locations:
        fields["locations"] = locations
    return fields


class _InterceptHandler(logging.Handler):
    """Passes records of the standard logging on to loguru, as an event ``log``."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        emit("log", level=level, logger=record.name, message=record.getMessage())


def _sink(stream: TextIO, render: Callable[["Record"], str]) -> Callable[["Message"], None]:
    def write(message: "Message") -> None:
        stream.write(render(message.record) + "\n")
        stream.flush()

    return write


def _event(record: "Record") -> dict[str, object]:
    event: dict[str, object] = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "event": record["message"],
    }
    event.update((key, value) for key, value in record["extra"].items() if key not in event)
    return event


def _render_json(record: "Record") -> str:
    return json.dumps(_event(record), default=str, ensure_ascii=False)


def _render_pretty(record: "Record") -> str:
    fields = " ".join(f"{key}={value}" for key, value in _flatten(record["extra"]))
    time_ = record["time"].strftime("%Y-%m-%d %H:%M:%S")
    return f"{time_} | {record['level'].name:<7} | {record['message']} {fields}".rstrip()


def _flatten(fields: Mapping[str, object], prefix: str = "") -> Iterator[tuple[str, object]]:
    for key, value in fields.items():
        if isinstance(value, Mapping):
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value
