from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from loguru import logger

if TYPE_CHECKING:
    from loguru import Message


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
