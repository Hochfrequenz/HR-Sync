"""
Guards of the integration tests against personal data in the test output: Personio is read in production, and so is
weclapp.

The modules of the integration tests are not rewritten by pytest (PYTEST_DONT_REWRITE in their docstrings), because
pytest would show the values of every subexpression of a failed assertion, e.g. a snapshot with real names. Instead,
every assertion has a message with employee numbers, field names, statuses and numbers only.
"""

import functools
from collections.abc import Callable
from typing import ParamSpec

import pytest

from hr_sync.anonymize import SUBSTITUTES
from hr_sync.events import error_fields
from hr_sync.model import Snapshot

P = ParamSpec("P")


def without_personal_data(test: Callable[P, None]) -> Callable[P, None]:
    """
    Reports a failure of the test, including its cleanup, only with values that are safe to log: a failed assertion
    with its message, any other error with its type and :func:`hr_sync.events.error_fields`. The original exception,
    its message, its traceback and chained exceptions are not shown, because they may contain personal data.
    """

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        failure: str | None = None
        try:
            test(*args, **kwargs)
        except AssertionError as error:
            failure = f"AssertionError: {error}"
        except Exception as error:  # e.g. an API error or a validation error that repeats a name
            failure = f"{type(error).__name__}: {error_fields(error)}"
        if failure is not None:
            # outside the except blocks, so that the original exception isn't chained to the failure
            pytest.fail(failure, pytrace=False)

    return run


def leaked(real: Snapshot, text: str) -> list[tuple[str, str]]:
    """
    Returns employee number and field of every real value that occurs in the text, without the value itself: names,
    e-mail addresses and phone numbers, i.e. the fields that the test mode substitutes.
    """
    return [
        (number, field)
        for number, record in real.records.items()
        for field in SUBSTITUTES
        if isinstance(value := getattr(record, field), str) and value in text
    ]
