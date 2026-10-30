"""
Fixtures of the integration tests. They read the real configuration: from the environment and from the ``.env`` file in
the root of the repository.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from hr_sync.config import (
    ConfigurationError,
    ConfigurationProblem,
    Environment,
    Settings,
    check_settings,
    load_settings,
)
from hr_sync.model import TargetSystem

ENV_FILE = Path(__file__).parents[2] / ".env"


class _Switches(BaseSettings):
    """The switches of the integration tests, read like the settings of HR-Sync."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_ignore_empty=True, extra="ignore")

    hr_sync_it_allow_writes: bool = False


@pytest.fixture(autouse=True)
def isolated_configuration() -> None:
    """
    Replaces the fixture of the unit tests that hides the configuration of the developer: the integration tests need it.
    """


@pytest.fixture(autouse=True)
def without_client_logs() -> Iterator[None]:
    """
    Turns off the standard logging of the client libraries: pytest would show their messages on a failure, and they may
    contain personal data from production. The events of HR-Sync use loguru and stay.
    """
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def test_settings() -> Settings:
    """
    The settings of the developer, always in the test mode with Decidalo as target: Personio is read in production,
    Decidalo only in its test environment. Skips the test if a credential is missing.
    """
    settings = load_settings(ENV_FILE if ENV_FILE.is_file() else None)
    settings = settings.model_copy(update={"hr_sync_environment": Environment.TEST})
    try:
        check_settings(settings, [TargetSystem.DECIDALO])
    except ConfigurationError as error:
        if error.problem == ConfigurationProblem.MISSING_VARIABLES:
            pytest.skip(f"missing {', '.join(error.variables)}")
        raise
    return settings


@pytest.fixture
def weclapp_settings() -> Settings:
    """
    The settings of the developer in the production mode with weclapp as target, as for a dry run: there is no weclapp
    test tenant. Skips the test if a credential is missing.
    """
    settings = load_settings(ENV_FILE if ENV_FILE.is_file() else None)
    settings = settings.model_copy(update={"hr_sync_environment": Environment.PRODUCTION})
    try:
        check_settings(settings, [TargetSystem.WECLAPP], dry_run=True)
    except ConfigurationError as error:
        if error.problem == ConfigurationProblem.MISSING_VARIABLES:
            pytest.skip(f"missing {', '.join(error.variables)}")
        raise
    return settings


@pytest.fixture
def allow_writes() -> None:
    """
    Skips the test unless ``HR_SYNC_IT_ALLOW_WRITES=1`` allows writing to the Decidalo test environment.
    """
    if not _Switches().hr_sync_it_allow_writes:
        pytest.skip("writing to the Decidalo test environment needs HR_SYNC_IT_ALLOW_WRITES=1")


@pytest.fixture
def keep_test_data(request: pytest.FixtureRequest) -> bool:
    """
    True with ``--keep-test-data``: the created user is kept, to look at it in Decidalo.
    """
    return bool(request.config.getoption("--keep-test-data"))
