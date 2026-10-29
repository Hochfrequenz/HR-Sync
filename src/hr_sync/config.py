"""
The configuration of HR-Sync, read from environment variables and a ``.env`` file.

Everything that depends on the company is a setting. The defaults are the settings of Hochfrequenz.
"""

from collections.abc import Collection, Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from hr_sync.events import LogFormat
from hr_sync.model import TargetSystem


class Environment(StrEnum):
    """Where HR-Sync writes to."""

    PRODUCTION = "production"
    TEST = "test"
    """test environments only get anonymized data; not available until the test mode exists (AP 8)"""


class Settings(BaseSettings):
    """
    The settings of HR-Sync. Each field is read from the environment variable with its name in upper case, e.g.
    ``personio_client_id`` from ``PERSONIO_CLIENT_ID``.

    Environment variables take precedence over the ``.env`` file. Empty values count as not set, so that a ``.env``
    copied from ``.env.example`` keeps the defaults. The credentials are optional here, because they are only required
    for the selected target systems (see :func:`check_settings`).
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore", frozen=True
    )

    personio_client_id: SecretStr | None = None
    personio_client_secret: SecretStr | None = None
    personio_app_id: str | None = "HR_SYNC"
    """identifies HR-Sync towards Personio"""
    personio_employee_number_attribute: str = "company_employee_id"
    """the id of the custom attribute that holds the employee number"""
    decidalo_api_key: SecretStr | None = None
    decidalo_employee_type_name: str = "Internal"
    """the employee type of new users"""
    decidalo_has_login: bool = True
    """whether new users get a login"""
    hr_sync_environment: Environment = Environment.PRODUCTION
    hr_sync_log_format: LogFormat = "pretty"


class ConfigurationProblem(StrEnum):
    """Why the configuration doesn't allow a run."""

    ENV_FILE_NOT_FOUND = "env_file_not_found"
    INVALID_VARIABLES = "invalid_variables"
    MISSING_VARIABLES = "missing_variables"
    TARGET_NOT_AVAILABLE = "target_not_available"
    """weclapp is selected, but its adapter doesn't exist yet (AP 5)"""
    TEST_MODE_NOT_AVAILABLE = "test_mode_not_available"
    """``HR_SYNC_ENVIRONMENT=test``, but the test mode doesn't exist yet (AP 8)"""


class ConfigurationError(Exception):
    """
    The configuration doesn't allow a run. The problem and the names of the variables are safe to log, their values
    are never part of the error.
    """

    def __init__(self, problem: ConfigurationProblem, variables: Iterable[str] = ()) -> None:
        self.problem = problem
        self.variables = tuple(variables)
        super().__init__(f"{problem}: {', '.join(self.variables)}" if self.variables else str(problem))

    @property
    def fields(self) -> dict[str, object]:
        """
        Returns the fields that describe the problem in an event.
        """
        fields: dict[str, object] = {"problem": self.problem}
        if self.variables:
            fields["variables"] = list(self.variables)
        return fields


def load_settings(env_file: Path | None = None) -> Settings:
    """
    Reads the settings from the environment and from ``env_file``, by default ``.env`` in the current directory.

    Raises:
        ConfigurationError: if ``env_file`` doesn't exist or a variable has an invalid value.
    """
    if env_file is not None and not env_file.is_file():
        raise ConfigurationError(ConfigurationProblem.ENV_FILE_NOT_FOUND)
    try:
        return Settings() if env_file is None else Settings(_env_file=env_file)
    except ValidationError as error:
        variables = sorted({str(detail["loc"][0]).upper() for detail in error.errors() if detail["loc"]})
        # without the validation error as cause: its message contains the values, e.g. secrets
        raise ConfigurationError(ConfigurationProblem.INVALID_VARIABLES, variables) from None


def check_settings(settings: Settings, targets: Collection[TargetSystem]) -> None:
    """
    Checks that the settings allow a run with the given target systems, before any system is called.

    Raises:
        ConfigurationError: if the test mode or weclapp is selected, which are not available yet, or if a credential
            of Personio or a selected target system is missing.
    """
    if settings.hr_sync_environment is Environment.TEST:
        raise ConfigurationError(ConfigurationProblem.TEST_MODE_NOT_AVAILABLE)
    if TargetSystem.WECLAPP in targets:
        raise ConfigurationError(ConfigurationProblem.TARGET_NOT_AVAILABLE)
    required = ["personio_client_id", "personio_client_secret"]
    if TargetSystem.DECIDALO in targets:
        required.append("decidalo_api_key")
    missing = [name.upper() for name in required if getattr(settings, name) is None]
    if missing:
        raise ConfigurationError(ConfigurationProblem.MISSING_VARIABLES, missing)
