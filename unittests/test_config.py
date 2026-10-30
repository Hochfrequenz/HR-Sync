from pathlib import Path

import pytest
from pydantic import SecretStr

from hr_sync.config import (
    DECIDALO_PRODUCTION_URL,
    ConfigurationError,
    ConfigurationProblem,
    Environment,
    Settings,
    check_settings,
    decidalo_test_url,
    load_settings,
)
from hr_sync.model import TargetSystem

_CREDENTIALS = """\
PERSONIO_CLIENT_ID=papi-id
PERSONIO_CLIENT_SECRET=papi-secret
DECIDALO_API_KEY=decidalo-key
"""


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "personio_client_id": SecretStr("papi-id"),
        "personio_client_secret": SecretStr("papi-secret"),
        "decidalo_api_key": SecretStr("decidalo-key"),
    }
    return Settings.model_validate(values | changes)


def _test_settings(**changes: object) -> Settings:
    """settings of the test mode, without the key of the production system"""
    values: dict[str, object] = {
        "hr_sync_environment": Environment.TEST,
        "decidalo_api_key": None,
        "decidalo_test_base_url": "https://test.decidalo.example",
        "decidalo_test_api_key": SecretStr("test-key"),
    }
    return _settings(**(values | changes))


class TestLoadSettings:
    """
    Tests of reading the settings from the environment and the .env file.
    """

    def test_defaults_of_hochfrequenz(self) -> None:
        settings = load_settings()
        assert settings.personio_client_id is None
        assert settings.personio_app_id == "HR_SYNC"
        assert settings.personio_employee_number_attribute == "company_employee_id"
        assert settings.personio_career_level_attribute == "dynamic_625934eadbe629.22673150"
        assert settings.personio_mobile_phone_attribute == "dynamic_6239d4b55575b2.63567609"
        assert settings.decidalo_employee_type_id == 2
        assert settings.decidalo_employee_type_name == "Internal"
        assert settings.decidalo_authorization_role_id == 2
        assert settings.decidalo_authorization_role_name == "Employee"
        assert (settings.decidalo_location, settings.decidalo_country_code) == ("DE", "DE")
        assert settings.decidalo_company_name == "Hochfrequenz Unternehmensberatung GmbH"
        assert settings.decidalo_has_login is True
        assert settings.hr_sync_environment == Environment.PRODUCTION
        assert settings.hr_sync_log_format == "pretty"

    def test_env_file_in_the_current_directory(self) -> None:
        Path(".env").write_text(_CREDENTIALS, encoding="utf-8")
        settings = load_settings()
        assert settings.personio_client_id == SecretStr("papi-id")
        assert settings.decidalo_api_key == SecretStr("decidalo-key")

    def test_given_env_file(self, tmp_path: Path) -> None:
        Path(".env").write_text("PERSONIO_APP_ID=FROM_CURRENT_DIRECTORY\n", encoding="utf-8")
        env_file = tmp_path / "config" / "hr-sync.env"
        env_file.parent.mkdir()
        env_file.write_text(_CREDENTIALS + "HR_SYNC_LOG_FORMAT=json\n", encoding="utf-8")
        settings = load_settings(env_file)
        assert settings.personio_client_secret == SecretStr("papi-secret")
        assert settings.hr_sync_log_format == "json"
        # the .env in the current directory is not read
        assert settings.personio_app_id == "HR_SYNC"

    def test_environment_variables_take_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        Path(".env").write_text(_CREDENTIALS, encoding="utf-8")
        monkeypatch.setenv("DECIDALO_API_KEY", "from-the-environment")
        assert load_settings().decidalo_api_key == SecretStr("from-the-environment")

    def test_empty_values_count_as_not_set(self) -> None:
        Path(".env").write_text("PERSONIO_CLIENT_ID=\nDECIDALO_EMPLOYEE_TYPE_NAME=\n", encoding="utf-8")
        settings = load_settings()
        assert settings.personio_client_id is None
        assert settings.decidalo_employee_type_name == "Internal"

    def test_example_configuration(self) -> None:
        settings = load_settings(Path(__file__).parents[1] / ".env.example")
        assert settings == Settings.model_validate({})

    def test_other_variables_are_ignored(self) -> None:
        Path(".env").write_text("WECLAPP_API_TOKEN=token\nDECIDALO_TEST_API_KEY=key\n", encoding="utf-8")
        assert load_settings() == Settings()

    def test_env_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            load_settings(tmp_path / "missing.env")
        assert error_info.value.problem == ConfigurationProblem.ENV_FILE_NOT_FOUND
        assert error_info.value.variables == ()

    def test_test_environment(self) -> None:
        Path(".env").write_text(
            "HR_SYNC_ENVIRONMENT=test\nDECIDALO_TEST_BASE_URL=https://test.decidalo.example\nDECIDALO_TEST_API_KEY=key\n",
            encoding="utf-8",
        )
        settings = load_settings()
        assert settings.test_mode
        assert str(settings.decidalo_test_base_url) == "https://test.decidalo.example/"
        assert settings.decidalo_test_api_key == SecretStr("key")

    def test_test_url_must_be_a_url(self) -> None:
        Path(".env").write_text("DECIDALO_TEST_BASE_URL=test.decidalo.example\n", encoding="utf-8")
        with pytest.raises(ConfigurationError) as error_info:
            load_settings()
        assert error_info.value.problem == ConfigurationProblem.INVALID_VARIABLES
        assert error_info.value.variables == ("DECIDALO_TEST_BASE_URL",)

    def test_invalid_values_name_only_the_variables(self) -> None:
        Path(".env").write_text(
            "HR_SYNC_ENVIRONMENT=prod\nDECIDALO_HAS_LOGIN=maybe-secret\nHR_SYNC_LOG_FORMAT=json\n", encoding="utf-8"
        )
        with pytest.raises(ConfigurationError) as error_info:
            load_settings()
        error = error_info.value
        assert error.problem == ConfigurationProblem.INVALID_VARIABLES
        assert error.variables == ("DECIDALO_HAS_LOGIN", "HR_SYNC_ENVIRONMENT")
        assert "maybe-secret" not in str(error)
        # the validation error, whose message contains the values, is not attached
        assert error.__cause__ is None
        assert error.__suppress_context__


class TestCheckSettings:
    """
    Tests of the checks before a run.
    """

    def test_complete(self) -> None:
        check_settings(_settings(), [TargetSystem.DECIDALO])

    def test_missing_credentials_of_personio(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(_settings(personio_client_id=None, personio_client_secret=None), [TargetSystem.DECIDALO])
        assert error_info.value.problem == ConfigurationProblem.MISSING_VARIABLES
        assert error_info.value.variables == ("PERSONIO_CLIENT_ID", "PERSONIO_CLIENT_SECRET")

    def test_decidalo_key_is_only_required_for_decidalo(self) -> None:
        settings = _settings(decidalo_api_key=None)
        check_settings(settings, [])
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(settings, [TargetSystem.DECIDALO])
        assert error_info.value.variables == ("DECIDALO_API_KEY",)

    def test_weclapp_is_not_available_yet(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(_settings(), list(TargetSystem))
        assert error_info.value.problem == ConfigurationProblem.TARGET_NOT_AVAILABLE

    def test_test_mode(self) -> None:
        # without the key of the production system
        check_settings(_test_settings(), [TargetSystem.DECIDALO])

    def test_test_mode_needs_the_test_environment(self) -> None:
        settings = _test_settings(decidalo_test_base_url=None, decidalo_test_api_key=None, decidalo_api_key="key")
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(settings, [TargetSystem.DECIDALO])
        assert error_info.value.problem == ConfigurationProblem.MISSING_VARIABLES
        assert error_info.value.variables == ("DECIDALO_TEST_BASE_URL", "DECIDALO_TEST_API_KEY")

    def test_production_mode_ignores_the_test_environment(self) -> None:
        settings = _settings(decidalo_api_key=None, decidalo_test_base_url=DECIDALO_PRODUCTION_URL)
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(settings, [TargetSystem.DECIDALO])
        assert error_info.value.variables == ("DECIDALO_API_KEY",)

    def test_production_url(self) -> None:
        # the default URL of the decidalo-client, import.decidalo.dev, is the test environment
        assert DECIDALO_PRODUCTION_URL == "https://import.decidalo.app"

    def test_test_environment_of_decidalo(self) -> None:
        check_settings(_test_settings(decidalo_test_base_url="https://import.decidalo.dev"), [TargetSystem.DECIDALO])

    def test_weclapp_is_not_allowed_in_test_mode(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(_test_settings(), list(TargetSystem))
        assert error_info.value.problem == ConfigurationProblem.TARGET_NOT_ALLOWED_IN_TEST_MODE

    @pytest.mark.parametrize(
        "url",
        [
            DECIDALO_PRODUCTION_URL,
            "https://IMPORT.decidalo.app/",
            "https://import.decidalo.app:443/importapi",
            "http://import.decidalo.app",
            # the trailing dot names the same host in the DNS
            "https://import.decidalo.app./",
            "https://IMPORT.decidalo.APP.:443",
        ],
    )
    def test_test_mode_never_writes_to_production(self, url: str) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_settings(_test_settings(decidalo_test_base_url=url), [TargetSystem.DECIDALO])
        assert error_info.value.problem == ConfigurationProblem.PRODUCTION_URL_IN_TEST_MODE
        assert error_info.value.variables == ("DECIDALO_TEST_BASE_URL",)


class TestDecidaloTestUrl:
    """
    Tests of the URL of the Decidalo test environment.
    """

    def test_url(self) -> None:
        assert decidalo_test_url(_test_settings()) == "https://test.decidalo.example/"

    def test_missing(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            decidalo_test_url(_test_settings(decidalo_test_base_url=None))
        assert error_info.value.problem == ConfigurationProblem.MISSING_VARIABLES


class TestConfigurationError:
    """
    Tests of the error that ends a run because of the configuration.
    """

    def test_with_variables(self) -> None:
        error = ConfigurationError(ConfigurationProblem.MISSING_VARIABLES, ["PERSONIO_CLIENT_ID"])
        assert str(error) == "missing_variables: PERSONIO_CLIENT_ID"
        assert error.fields == {"problem": "missing_variables", "variables": ["PERSONIO_CLIENT_ID"]}

    def test_without_variables(self) -> None:
        error = ConfigurationError(ConfigurationProblem.TARGET_NOT_AVAILABLE)
        assert str(error) == "target_not_available"
        assert error.fields == {"problem": "target_not_available"}
