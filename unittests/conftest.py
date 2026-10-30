import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from hr_sync.config import Settings

if TYPE_CHECKING:
    from loguru import Message


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-test-data",
        action="store_true",
        help="keep the user that the integration test creates in the Decidalo test environment, instead of deleting it",
    )


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Keeps the tests away from the configuration of the developer: no environment variable of HR-Sync is set, and the
    current directory has no ``.env`` file. So no test can reach a real system with real credentials.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def events() -> Iterator[list[dict[str, object]]]:
    """Collects the events logged during a test, each as a dict of its name, level and fields."""
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
