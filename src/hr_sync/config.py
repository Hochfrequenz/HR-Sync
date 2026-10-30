"""
The configuration of HR-Sync, read from environment variables and a ``.env`` file.

Everything that depends on the company is a setting. The defaults are the settings of Hochfrequenz.
"""

from collections.abc import Collection, Iterable
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from hr_sync.events import LogFormat
from hr_sync.model import TargetSystem

DECIDALO_PRODUCTION_URL = "https://import.decidalo.app"
"""
the production system of Decidalo, which the production mode always uses and the test mode never; the default URL of
the decidalo-client, ``https://import.decidalo.dev``, is the test environment
"""


class Environment(StrEnum):
    """Where HR-Sync writes to."""

    PRODUCTION = "production"
    TEST = "test"
    """
    the test mode: the data from Personio is anonymized, and only test environments are written to, i.e. the Decidalo
    test environment; there is no weclapp test tenant
    """


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
    personio_career_level_attribute: str = "dynamic_625934eadbe629.22673150"
    """the id of the custom attribute that holds the career level"""
    personio_mobile_phone_attribute: str = "dynamic_6239d4b55575b2.63567609"
    """the id of the custom attribute that holds the mobile phone number"""
    decidalo_api_key: SecretStr | None = None
    decidalo_employee_type_id: int = 2
    """the employee type of new users, by id; it wins over the name if the name doesn't exist"""
    decidalo_employee_type_name: str = "Internal"
    decidalo_authorization_role_id: int = 2
    """the authorization role of new users, by id and name"""
    decidalo_authorization_role_name: str = "Employee"
    decidalo_location: str = "DE"
    decidalo_country_code: str = "DE"
    decidalo_company_name: str = "Hochfrequenz Unternehmensberatung GmbH"
    decidalo_has_login: bool = True
    """whether new users get a login; in the test mode, they never get one"""
    decidalo_test_base_url: AnyHttpUrl | None = None
    """the Decidalo test environment, which only the test mode uses"""
    decidalo_test_api_key: SecretStr | None = None
    hr_sync_environment: Environment = Environment.PRODUCTION
    hr_sync_log_format: LogFormat = "pretty"

    @property
    def test_mode(self) -> bool:
        """
        True with ``HR_SYNC_ENVIRONMENT=test``, see :attr:`Environment.TEST`.
        """
        return self.hr_sync_environment == Environment.TEST


class ConfigurationProblem(StrEnum):
    """Why the configuration doesn't allow a run."""

    ENV_FILE_NOT_FOUND = "env_file_not_found"
    INVALID_VARIABLES = "invalid_variables"
    MISSING_VARIABLES = "missing_variables"
    TARGET_NOT_AVAILABLE = "target_not_available"
    """weclapp is selected, but its adapter doesn't exist yet (AP 5)"""
    TARGET_NOT_ALLOWED_IN_TEST_MODE = "target_not_allowed_in_test_mode"
    """weclapp is selected in the test mode, but has no test tenant"""
    PRODUCTION_URL_IN_TEST_MODE = "production_url_in_test_mode"
    """``DECIDALO_TEST_BASE_URL`` points to the production system"""


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

    The production mode needs the credentials of the production systems, the test mode those of the test
    environments: ``DECIDALO_TEST_BASE_URL`` and ``DECIDALO_TEST_API_KEY`` instead of ``DECIDALO_API_KEY``.

    Raises:
        ConfigurationError: if weclapp is selected, which is not available yet and never in the test mode, if a
            credential of Personio or a selected target system is missing, or if the test mode would write to the
            production system of Decidalo.
    """
    if settings.test_mode and TargetSystem.WECLAPP in targets:
        raise ConfigurationError(ConfigurationProblem.TARGET_NOT_ALLOWED_IN_TEST_MODE)
    if TargetSystem.WECLAPP in targets:
        raise ConfigurationError(ConfigurationProblem.TARGET_NOT_AVAILABLE)
    required = ["personio_client_id", "personio_client_secret"]
    if TargetSystem.DECIDALO in targets:
        required.extend(
            ["decidalo_test_base_url", "decidalo_test_api_key"] if settings.test_mode else ["decidalo_api_key"]
        )
    missing = [name.upper() for name in required if getattr(settings, name) is None]
    if missing:
        raise ConfigurationError(ConfigurationProblem.MISSING_VARIABLES, missing)
    if settings.test_mode and TargetSystem.DECIDALO in targets:
        decidalo_test_url(settings)


def decidalo_test_url(settings: Settings) -> str:
    """
    Returns the URL of the Decidalo test environment.

    It must not point to the production system (compared by the host), so that the test mode never writes anonymized
    data or users without login to production.

    Raises:
        ConfigurationError: if ``DECIDALO_TEST_BASE_URL`` is missing or points to the production system.
    """
    url = settings.decidalo_test_base_url
    if url is None:
        raise ConfigurationError(ConfigurationProblem.MISSING_VARIABLES, ["DECIDALO_TEST_BASE_URL"])
    # a trailing dot names the same host in the DNS, e.g. import.decidalo.app.
    if (url.host or "").rstrip(".") == urlsplit(DECIDALO_PRODUCTION_URL).hostname:
        raise ConfigurationError(ConfigurationProblem.PRODUCTION_URL_IN_TEST_MODE, ["DECIDALO_TEST_BASE_URL"])
    return str(url)
