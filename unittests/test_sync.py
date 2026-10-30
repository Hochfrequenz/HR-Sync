from collections.abc import Callable
from datetime import date
from unittest.mock import ANY

import pytest
from decidalo_client import DecidaloClient
from decidalo_client.models import ImportItemStatusType, UserImportResults
from personio_client import Employment, Person, PersonioClient
from pydantic import SecretStr
from weclapp_client import WeclappClient
from weclapp_client.models import UserStatus
from weclapp_client.testing import FakeWeclapp

from hr_sync.config import ConfigurationError, ConfigurationProblem, Environment, Settings
from hr_sync.model import EmployeeRecord, NoticeReason, System, TargetSystem
from hr_sync.plan import EmployeePlan, Operation, OperationType, Outcome, SyncPlan
from hr_sync.sync import (
    ExitCode,
    RunOptions,
    Systems,
    connect,
    create_plan,
    execute_plan,
    load_snapshots,
    new_decidalo_users,
    run,
    weclapp_setup,
)
from hr_sync.systems.decidalo import NewUserDefaults
from hr_sync.systems.weclapp import WeclappSetup, new_employee
from unittests.fakes import (
    ATTRIBUTE,
    PHONE,
    FakeDecidalo,
    FakePersonio,
    decidalo_user,
    import_row,
    personio_employment,
    personio_person,
    weclapp_fake,
)

TODAY = date(2026, 10, 29)
DECIDALO = RunOptions(targets=(TargetSystem.DECIDALO,))
"""a run with Decidalo as the only target, as long as the weclapp adapter is missing"""

ERIKA = personio_person("1", "00165")
MAX = personio_person("2", "00200", email="max.mustermann@example.com", first_name="Max", last_name="Mustermann")
START = date(2026, 9, 1)
"""the start of the employments in Personio"""
MAX_IN_DECIDALO = decidalo_user(
    7, "00200", email="max.mustermann@example.com", display_name="Max Mustermann", entry_date=START
)
PERSONAL_DATA = ("Erika", "Musterfrau", "Max", "Mustermann", "@example.com")


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "personio_client_id": SecretStr("papi-id"),
        "personio_client_secret": SecretStr("papi-secret"),
        "decidalo_api_key": SecretStr("decidalo-key"),
    }
    return Settings.model_validate(values | changes)


def _weclapp_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "weclapp_api_token": SecretStr("weclapp-token"),
        "weclapp_tenant": "hf",
        "weclapp_employee_number_attribute_id": "7621",
    }
    return _settings(**(values | changes))


def _test_settings(**changes: object) -> Settings:
    """settings of the test mode, with a login for new users that the test mode must ignore"""
    values: dict[str, object] = {
        "hr_sync_environment": Environment.TEST,
        "decidalo_api_key": None,
        "decidalo_test_base_url": "https://test.decidalo.example",
        "decidalo_test_api_key": SecretStr("test-key"),
        "decidalo_has_login": True,
    }
    return _settings(**(values | changes))


def _personio(*persons: Person, employments: list[Employment] | None = None) -> FakePersonio:
    return FakePersonio([(person, employments or [personio_employment()]) for person in persons])


def _run(
    personio: FakePersonio, decidalo: FakeDecidalo, options: RunOptions = DECIDALO, settings: Settings | None = None
) -> ExitCode:
    systems = Systems(personio=personio, decidalo=decidalo)
    return run(settings or _settings(), options, systems=systems, today=TODAY)


def _named(events: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [event for event in events if event["event"] == name]


def _run_event(events: list[dict[str, object]]) -> dict[str, object]:
    (event,) = _named(events, "sync.run")
    return event


class TestRun:
    """
    Tests of whole runs against fake clients.
    """

    def test_creates_the_missing_employees(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo(users=[MAX_IN_DECIDALO])
        assert _run(_personio(ERIKA, MAX), decidalo) == ExitCode.SUCCESS
        assert [[user.employeeID for user in batch] for batch in decidalo.batches] == [["00165"]]
        assert [(event["event"], event.get("system")) for event in events] == [
            ("sync.load", System.PERSONIO),
            ("sync.load", System.DECIDALO),
            ("sync.plan", None),
            ("sync.operation", TargetSystem.DECIDALO),
            ("sync.run", None),
        ]
        run_event = _run_event(events)
        assert run_event["level"] == "INFO"
        assert run_event["outcome"] == "success"
        assert run_event["targets"] == ["decidalo"]
        assert "only" not in run_event
        assert run_event["results"] == {"decidalo": {"create": {"success": 1}}}
        assert (run_event["notices"], run_event["errors"], run_event["exit_code"]) == (0, 0, 0)
        # every event belongs to the run
        assert len({event["run_id"] for event in events}) == 1
        assert {(event["environment"], event["dry_run"]) for event in events} == {(Environment.PRODUCTION, False)}
        assert not any(value in str(events) for value in PERSONAL_DATA)

    def test_second_run_writes_nothing(self, events: list[dict[str, object]]) -> None:
        personio, decidalo = _personio(ERIKA, MAX), FakeDecidalo()
        assert _run(personio, decidalo) == ExitCode.SUCCESS
        assert len(decidalo.batches) == 2
        events.clear()
        assert _run(personio, decidalo) == ExitCode.SUCCESS
        assert len(decidalo.batches) == 2
        (plan_event,) = _named(events, "sync.plan")
        assert plan_event["is_empty"] is True
        assert _named(events, "sync.deviation") == []
        assert _run_event(events)["results"] == {}

    def test_dry_run_writes_nothing(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo()
        options = RunOptions(targets=(TargetSystem.DECIDALO,), dry_run=True)
        assert _run(_personio(ERIKA, MAX), decidalo, options) == ExitCode.SUCCESS
        assert decidalo.batches == []
        assert [(event["employee_number"], event["outcome"]) for event in _named(events, "sync.operation")] == [
            ("00165", Outcome.DRY_RUN),
            ("00200", Outcome.DRY_RUN),
        ]
        run_event = _run_event(events)
        assert run_event["dry_run"] is True
        assert run_event["results"] == {"decidalo": {"create": {"dry_run": 2}}}

    def test_only(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo()
        options = RunOptions(targets=(TargetSystem.DECIDALO,), only=frozenset({"00200", "00999"}))
        assert _run(_personio(ERIKA, MAX), decidalo, options) == ExitCode.SUCCESS
        # all employees are loaded, but only the given ones are planned
        assert decidalo.users[0].employeeID == "00200"
        assert [[user.employeeID for user in batch] for batch in decidalo.batches] == [["00200"]]
        assert [(event["reason"], event["employee_number"]) for event in _named(events, "sync.notice")] == [
            ("unknown_employee_number", "00999")
        ]
        run_event = _run_event(events)
        assert run_event["only"] == ["00200", "00999"]
        assert run_event["notices"] == 1

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_max_employees(self, events: list[dict[str, object]], dry_run: bool) -> None:
        decidalo = FakeDecidalo()
        options = RunOptions(targets=(TargetSystem.DECIDALO,), max_employees=1, dry_run=dry_run)
        assert _run(_personio(ERIKA, MAX), decidalo, options) == ExitCode.SUCCESS
        assert [event["employee_number"] for event in _named(events, "sync.operation")] == ["00165"]
        assert len(decidalo.batches) == (0 if dry_run else 1)
        (plan_event,) = _named(events, "sync.plan")
        assert plan_event["limit_reached"] is True
        assert _run_event(events)["max_employees"] == 1

    def test_test_mode(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo()
        assert _run(_personio(ERIKA, MAX), decidalo, settings=_test_settings()) == ExitCode.SUCCESS
        payload = [user.model_dump(exclude_none=True) for batch in decidalo.batches for user in batch]
        assert payload == [
            {
                "employeeID": number,
                "displayName": f"Test Person-{number}",
                "email": f"test-{number}@example.com",
                "entryDate": START,
                "employeeTypeID": 2,
                "employeeTypeName": "Internal",
                "authorizationRoleID": 2,
                "authorizationRoleName": "Employee",
                "location": "DE",
                "countryCode": "DE",
                "companyName": "Hochfrequenz Unternehmensberatung GmbH",
                "includeInResourceManagement": True,
                "hasLogin": False,
                "delete": False,
                # the fields of phase 2: only the phone number is substituted
                "jobTitle": "Senior Consultant",
                "serviceCategoryName": "Senior Consultant",
                "mobilePhoneNumber": f"+49 000 {number}",
                "holidayListCode": "DE-BY",
                "standardWeeklyHours": 40,
            }
            for number in ("00165", "00200")
        ]
        real = ("Erika", "Musterfrau", "Max", "Mustermann", "erika.musterfrau", "max.mustermann", PHONE)
        assert [value for value in real if value in f"{payload} {events}"] == []
        assert {event["environment"] for event in events} == {Environment.TEST}

    def test_second_run_in_the_test_mode_writes_nothing(self, events: list[dict[str, object]]) -> None:
        personio, decidalo = _personio(ERIKA, MAX), FakeDecidalo()
        assert _run(personio, decidalo, settings=_test_settings()) == ExitCode.SUCCESS
        assert len(decidalo.batches) == 2
        events.clear()
        assert _run(personio, decidalo, settings=_test_settings()) == ExitCode.SUCCESS
        assert len(decidalo.batches) == 2
        # the substitutes only depend on the employee number, so they don't deviate
        assert _named(events, "sync.deviation") == []

    def test_notices_and_deviations(self, events: list[dict[str, object]]) -> None:
        without_email = personio_person("3", "00300", email=None)
        renamed = decidalo_user(8, "00165", display_name="Erika Mustermann", entry_date=START)
        decidalo = FakeDecidalo(users=[renamed, decidalo_user(9, None, email="admin@example.com")])
        assert _run(_personio(ERIKA, without_email), decidalo) == ExitCode.SUCCESS
        assert decidalo.batches == []
        assert [(event["level"], event["system"], event["reason"]) for event in _named(events, "sync.notice")] == [
            ("WARNING", "personio", "missing_email"),
            ("WARNING", "decidalo", "missing_employee_number"),
        ]
        assert [
            (event["employee_number"], event["system"], event["fields"]) for event in _named(events, "sync.deviation")
        ] == [("00165", TargetSystem.DECIDALO, ["display_name"])]
        assert _run_event(events)["notices"] == 2
        assert not any(value in str(events) for value in PERSONAL_DATA)

    def test_failed_operation(self, events: list[dict[str, object]]) -> None:
        failed = UserImportResults(items=[import_row("00165", ImportItemStatusType.Failed)])
        decidalo = FakeDecidalo(responses=[failed])
        assert _run(_personio(ERIKA), decidalo) == ExitCode.OPERATION_FAILED
        run_event = _run_event(events)
        assert run_event["level"] == "ERROR"
        assert run_event["outcome"] == "error"
        assert "error" not in run_event
        assert run_event["results"] == {"decidalo": {"create": {"error": 1}}}
        assert (run_event["errors"], run_event["exit_code"]) == (1, 1)

    def test_list_cut_by_decidalo_aborts_before_writing(self, events: list[dict[str, object]]) -> None:
        # Max exists, but Decidalo returns only the first user: he must not be "created", i.e. overwritten
        decidalo = FakeDecidalo(users=[decidalo_user(6, "00100", email="other@example.com"), MAX_IN_DECIDALO], limit=1)
        assert _run(_personio(ERIKA, MAX), decidalo) == ExitCode.ABORTED
        assert decidalo.batches == []
        assert _named(events, "sync.plan") == []
        error = _run_event(events)["error"]
        assert isinstance(error, dict)
        assert error["type"] == "IncompleteUserListError"

    def test_error_while_loading_aborts_before_planning(self, events: list[dict[str, object]]) -> None:
        decidalo = FakeDecidalo(fail_get=True)
        assert _run(_personio(ERIKA), decidalo) == ExitCode.ABORTED
        assert decidalo.batches == []
        assert [event["event"] for event in events] == ["sync.load", "sync.load", "sync.run"]
        run_event = _run_event(events)
        assert run_event["level"] == "ERROR"
        assert run_event["error"] == {
            "type": "DecidaloAPIError",
            "status_code": 401,
            "module": "hr_sync.systems.decidalo",
            "function": "_fetch_users",
            "line": ANY,
        }
        assert run_event["exit_code"] == 2

    @pytest.mark.parametrize(
        ("options", "settings", "error"),
        [
            pytest.param(
                RunOptions(),
                _settings,
                {
                    "problem": "missing_variables",
                    "variables": [
                        "WECLAPP_API_TOKEN",
                        "WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID",
                        "WECLAPP_TENANT",
                        "WECLAPP_BASE_URL",
                    ],
                },
                id="weclapp",
            ),
            pytest.param(
                RunOptions(),
                _test_settings,
                {"problem": "target_not_allowed_in_test_mode"},
                id="weclapp in the test mode",
            ),
            pytest.param(
                DECIDALO,
                lambda: _test_settings(decidalo_test_base_url="https://import.decidalo.app"),
                {
                    "function": "decidalo_test_url",
                    "problem": "production_url_in_test_mode",
                    "variables": ["DECIDALO_TEST_BASE_URL"],
                },
                id="production in the test mode",
            ),
            pytest.param(
                DECIDALO,
                lambda: _settings(decidalo_api_key=None),
                {"problem": "missing_variables", "variables": ["DECIDALO_API_KEY"]},
                id="missing credential",
            ),
        ],
    )
    def test_configuration_aborts_before_any_system_is_called(
        self,
        events: list[dict[str, object]],
        options: RunOptions,
        settings: Callable[[], Settings],
        error: dict[str, object],
    ) -> None:
        personio, decidalo = _personio(ERIKA), FakeDecidalo()
        assert _run(personio, decidalo, options, settings()) == ExitCode.ABORTED
        assert not personio.opened
        assert decidalo.opened == 0
        run_event = _run_event(events)
        assert run_event["error"] == {
            "type": "ConfigurationError",
            "module": "hr_sync.config",
            "function": "check_settings",
            "line": ANY,
            **error,
        }
        assert run_event["exit_code"] == 2

    def test_weclapp_without_client_is_a_programming_error(self, events: list[dict[str, object]]) -> None:
        options = RunOptions(targets=(TargetSystem.WECLAPP,), dry_run=True)
        assert run(_weclapp_settings(), options, systems=Systems(personio=_personio(ERIKA)), today=TODAY) == (
            ExitCode.ABORTED
        )
        error = _run_event(events)["error"]
        assert isinstance(error, dict)
        assert (error["type"], error["function"]) == ("ValueError", "_weclapp")

    def test_unexpected_error_is_logged_with_its_code_location(self, events: list[dict[str, object]]) -> None:
        # a programming error: Decidalo is selected, but has no client
        systems = Systems(personio=_personio(ERIKA))
        assert run(_settings(), DECIDALO, systems=systems, today=TODAY) == ExitCode.ABORTED
        assert _run_event(events)["error"] == {
            "type": "ValueError",
            "module": "hr_sync.sync",
            "function": "_decidalo",
            "line": ANY,
        }

    def test_connects_to_the_systems_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        decidalo = FakeDecidalo()
        connected: list[tuple[TargetSystem, ...]] = []

        def fake_connect(settings: Settings, targets: tuple[TargetSystem, ...]) -> Systems:
            connected.append(targets)
            return Systems(personio=_personio(ERIKA), decidalo=decidalo)

        monkeypatch.setattr("hr_sync.sync.connect", fake_connect)
        assert run(_settings(), DECIDALO) == ExitCode.SUCCESS
        assert connected == [(TargetSystem.DECIDALO,)]
        assert len(decidalo.batches) == 1


class TestRunWithWeclapp:
    """
    Tests of whole runs with weclapp as target, against the fake of the weclapp-client. Until the client can send the
    phone number, weclapp only runs as a dry run.
    """

    @pytest.fixture
    def fake(self, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeWeclapp, WeclappSetup]:
        fake, setup = weclapp_fake()
        # the fake assigns its own ids instead of the ids of Hochfrequenz
        monkeypatch.setattr("hr_sync.sync.weclapp_setup", lambda settings: setup)
        return fake, setup

    def _run(
        self,
        fake: FakeWeclapp,
        *persons: Person,
        dry_run: bool = True,
        targets: tuple[TargetSystem, ...] = (TargetSystem.WECLAPP,),
    ) -> ExitCode:
        with fake.client() as client:
            systems = Systems(personio=_personio(*persons), decidalo=FakeDecidalo(), weclapp=client)
            options = RunOptions(targets=targets, dry_run=dry_run)
            return run(_weclapp_settings(), options, systems=systems, today=TODAY)

    def test_dry_run(self, fake: tuple[FakeWeclapp, WeclappSetup], events: list[dict[str, object]]) -> None:
        weclapp, _ = fake
        assert self._run(weclapp, ERIKA, MAX) == ExitCode.SUCCESS
        assert weclapp.write_requests == []
        operations = _named(events, "sync.operation")
        assert [(event["system"], event["type"], event["outcome"]) for event in operations] == [
            ("weclapp", "create", "dry_run"),
            ("weclapp", "create", "dry_run"),
        ]
        assert [value for value in PERSONAL_DATA if value in str(events)] == []

    def test_creates_the_missing_employees(
        self, fake: tuple[FakeWeclapp, WeclappSetup], events: list[dict[str, object]]
    ) -> None:
        weclapp, _ = fake
        assert self._run(weclapp, ERIKA, MAX, dry_run=False) == ExitCode.SUCCESS
        assert sorted(user.email or "" for user in weclapp.users) == [
            "erika.musterfrau@example.com",
            "max.mustermann@example.com",
        ]
        assert {user.mobile_phone_number for user in weclapp.users} == {"+49 151 27409444"}
        assert len(weclapp.employees) == 2
        operations = _named(events, "sync.operation")
        assert [(event["system"], event["outcome"]) for event in operations] == [
            ("weclapp", "success"),
            ("weclapp", "success"),
        ]
        assert [value for value in PERSONAL_DATA if value in str(events)] == []

    def test_second_run_writes_nothing(
        self, fake: tuple[FakeWeclapp, WeclappSetup], events: list[dict[str, object]]
    ) -> None:
        weclapp, _ = fake
        assert self._run(weclapp, ERIKA, MAX, dry_run=False) == ExitCode.SUCCESS
        weclapp.clear_requests()
        events.clear()
        assert self._run(weclapp, ERIKA, MAX, dry_run=False) == ExitCode.SUCCESS
        assert weclapp.write_requests == []
        # what HR-Sync wrote is read back without deviations
        assert _named(events, "sync.deviation") == []

    def test_user_without_employee_record_is_completed_in_the_next_run(
        self, fake: tuple[FakeWeclapp, WeclappSetup], events: list[dict[str, object]]
    ) -> None:
        weclapp, _ = fake
        weclapp.fail_next(400, method="POST", path="employee")
        assert self._run(weclapp, ERIKA, dry_run=False) == ExitCode.OPERATION_FAILED
        assert (len(weclapp.users), len(weclapp.employees)) == (1, 0)
        assert self._run(weclapp, ERIKA, dry_run=False) == ExitCode.SUCCESS
        (user,) = weclapp.users
        assert [employee.user_id for employee in weclapp.employees] == [user.id]

    def test_user_without_employee_record_is_completed(
        self, fake: tuple[FakeWeclapp, WeclappSetup], events: list[dict[str, object]]
    ) -> None:
        weclapp, setup = fake
        record = load_snapshots(Systems(personio=_personio(ERIKA)), _settings(), [], TODAY).source.records["00165"]
        operation = Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=record)
        with weclapp.client() as client:
            user = client.users.create(new_employee(operation, setup).user)  # type: ignore[arg-type]
        assert self._run(weclapp, ERIKA) == ExitCode.SUCCESS
        # the entry date and the weekly hours come with the employee record, so they are not compared
        operations = _named(events, "sync.operation")
        assert [(event["type"], event["target_id"]) for event in operations] == [("complete", user.id)]
        assert _named(events, "sync.deviation") == []

    def test_deactivated_user_blocks_the_employee(
        self, fake: tuple[FakeWeclapp, WeclappSetup], events: list[dict[str, object]]
    ) -> None:
        weclapp, setup = fake
        weclapp.add_user(
            email="erika.musterfrau@example.com",
            status=UserStatus.DEPARTURE,
            custom_attributes={setup.employee_number_attribute_id: "00165"},
        )
        targets = (TargetSystem.WECLAPP, TargetSystem.DECIDALO)
        assert self._run(weclapp, ERIKA, targets=targets) == ExitCode.SUCCESS
        assert _named(events, "sync.operation") == []
        notices = _named(events, "sync.notice")
        assert [(event["system"], event["reason"]) for event in notices] == [("weclapp", "deactivated_in_target")]

    def test_setup_that_doesnt_fit_aborts(
        self, monkeypatch: pytest.MonkeyPatch, events: list[dict[str, object]]
    ) -> None:
        setup = WeclappSetup(employee_number_attribute_id="7621")
        monkeypatch.setattr("hr_sync.sync.weclapp_setup", lambda settings: setup)
        assert self._run(FakeWeclapp(), ERIKA) == ExitCode.ABORTED
        error = _run_event(events)["error"]
        assert isinstance(error, dict)
        assert error["problem"] == "weclapp_setup_mismatch"
        assert error["variables"] == ["WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID"]
        assert isinstance(error["ids"], list)
        assert "8211" in error["ids"]

    def test_connected_weclapp_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed: list[bool] = []
        client = FakeWeclapp().client()
        monkeypatch.setattr(client, "close", lambda: closed.append(True))
        systems = Systems(personio=_personio(), weclapp=client)
        monkeypatch.setattr("hr_sync.sync.connect", lambda settings, targets: systems)
        monkeypatch.setattr("hr_sync.sync.load_snapshots", _fail)
        options = RunOptions(targets=(TargetSystem.WECLAPP,), dry_run=True)
        assert run(_weclapp_settings(), options) == ExitCode.ABORTED
        assert closed == [True]


def _fail(*args: object) -> None:
    raise ConnectionError("not reachable")


class TestWeclappSetup:
    """
    Tests of the ids in weclapp from the settings.
    """

    def test_from_the_settings(self) -> None:
        setup = weclapp_setup(_weclapp_settings(weclapp_new_user_status=UserStatus.NOT_ACTIVE))
        assert setup == WeclappSetup(employee_number_attribute_id="7621", new_user_status=UserStatus.NOT_ACTIVE)

    def test_attribute_of_the_employee_number_is_required(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            weclapp_setup(_settings())
        assert error_info.value.variables == ("WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID",)


class TestNewDecidaloUsers:
    """
    Tests of the values that every new user gets in Decidalo.
    """

    def test_from_the_settings(self) -> None:
        settings = _settings(
            decidalo_employee_type_id=7,
            decidalo_employee_type_name="Werkstudent",
            decidalo_authorization_role_id=3,
            decidalo_authorization_role_name="Resource Manager",
            decidalo_location="AT",
            decidalo_country_code="AT",
            decidalo_company_name="Beispiel GmbH",
            decidalo_has_login=False,
        )
        assert new_decidalo_users(settings) == NewUserDefaults(
            employee_type_id=7,
            employee_type_name="Werkstudent",
            authorization_role_id=3,
            authorization_role_name="Resource Manager",
            location="AT",
            country_code="AT",
            company_name="Beispiel GmbH",
            has_login=False,
        )

    def test_no_login_in_the_test_mode(self) -> None:
        assert new_decidalo_users(_settings(decidalo_has_login=True)).has_login is True
        assert new_decidalo_users(_test_settings(decidalo_has_login=True)).has_login is False


class TestConnect:
    """
    Tests of creating the clients from the settings.
    """

    def test_clients(self) -> None:
        systems = connect(_settings(), [TargetSystem.DECIDALO])
        assert isinstance(systems.personio, PersonioClient)
        assert isinstance(systems.decidalo, DecidaloClient)
        # the production system, which can't be configured; the default URL of the client is the test environment
        assert systems.decidalo._base_url == "https://import.decidalo.app"

    def test_production_mode_ignores_the_test_environment(self) -> None:
        systems = connect(_settings(decidalo_test_base_url="https://import.decidalo.dev"), [TargetSystem.DECIDALO])
        assert isinstance(systems.decidalo, DecidaloClient)
        assert systems.decidalo._base_url == "https://import.decidalo.app"

    def test_test_environment_in_the_test_mode(self) -> None:
        systems = connect(_test_settings(), [TargetSystem.DECIDALO])
        assert isinstance(systems.decidalo, DecidaloClient)
        assert systems.decidalo._base_url == "https://test.decidalo.example"
        assert systems.decidalo._api_key == "test-key"

    def test_test_mode_never_connects_to_production(self) -> None:
        # also when connect is called without check_settings, e.g. by a test
        settings = _test_settings(decidalo_test_base_url="https://import.decidalo.app/")
        with pytest.raises(ConfigurationError) as error_info:
            connect(settings, [TargetSystem.DECIDALO])
        assert error_info.value.problem == ConfigurationProblem.PRODUCTION_URL_IN_TEST_MODE

    def test_only_selected_targets_get_a_client(self) -> None:
        systems = connect(_settings(decidalo_api_key=None), [])
        assert (systems.decidalo, systems.weclapp) == (None, None)

    @pytest.mark.parametrize(
        ("changes", "base_url"),
        [
            pytest.param({}, "https://hf.weclapp.com/webapp/api/v2/", id="tenant"),
            pytest.param(
                {"weclapp_base_url": "https://weclapp.example/webapp/api/v2"},
                "https://weclapp.example/webapp/api/v2/",
                id="base URL wins",
            ),
        ],
    )
    def test_weclapp(self, changes: dict[str, object], base_url: str) -> None:
        systems = connect(_weclapp_settings(**changes), [TargetSystem.WECLAPP])
        assert isinstance(systems.weclapp, WeclappClient)
        assert systems.weclapp._transport._config.base_url == base_url
        systems.close()

    def test_weclapp_without_tenant(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            connect(_weclapp_settings(weclapp_tenant=None), [TargetSystem.WECLAPP])
        assert error_info.value.variables == ("WECLAPP_TENANT", "WECLAPP_BASE_URL")

    def test_weclapp_never_in_the_test_mode(self) -> None:
        # also when connect is called without check_settings: there is no weclapp test tenant
        with pytest.raises(ConfigurationError) as error_info:
            connect(_weclapp_settings(hr_sync_environment=Environment.TEST), [TargetSystem.WECLAPP])
        assert error_info.value.problem == ConfigurationProblem.TARGET_NOT_ALLOWED_IN_TEST_MODE

    def test_missing_credential(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            connect(_settings(personio_client_id=None), [])
        assert error_info.value.problem == ConfigurationProblem.MISSING_VARIABLES
        assert error_info.value.variables == ("PERSONIO_CLIENT_ID",)


class TestSteps:
    """
    Tests of the steps of a run that a run with Decidalo as the only target doesn't reach.
    """

    def test_create_plan_event(self, events: list[dict[str, object]]) -> None:
        snapshots = load_snapshots(
            Systems(personio=_personio(ERIKA, MAX), decidalo=FakeDecidalo(users=[MAX_IN_DECIDALO])),
            _settings(),
            [TargetSystem.DECIDALO],
            TODAY,
        )
        events.clear()
        create_plan(snapshots)
        (plan_event,) = events
        assert plan_event["event"] == "sync.plan"
        assert plan_event["operations"] == {"decidalo": {"create": 1}}
        assert plan_event["deviations"] == {"decidalo": 0}
        assert plan_event["only_in_target"] == {"decidalo": 0}
        assert (plan_event["notices"], plan_event["is_empty"], plan_event["limit_reached"]) == (0, False, False)

    def test_load_weclapp(self, monkeypatch: pytest.MonkeyPatch, events: list[dict[str, object]]) -> None:
        fake, setup = weclapp_fake()
        monkeypatch.setattr("hr_sync.sync.weclapp_setup", lambda settings: setup)
        with fake.client() as client:
            snapshots = load_snapshots(
                Systems(personio=_personio(ERIKA), weclapp=client), _settings(), [TargetSystem.WECLAPP], TODAY
            )
        assert list(snapshots.targets) == [TargetSystem.WECLAPP]
        assert [(event["event"], event["system"]) for event in events] == [
            ("sync.load", "personio"),
            ("sync.load", "weclapp"),
        ]

    def test_weclapp_dry_run(self, events: list[dict[str, object]]) -> None:
        record = EmployeeRecord(employee_number="00165", first_name="Erika")
        operation = Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=record)
        sync_plan = SyncPlan(
            employees=(EmployeePlan(employee_number="00165", operations=(operation,)),),
            only_in_target={TargetSystem.WECLAPP: 0},
        )
        systems = Systems(personio=_personio())
        results = execute_plan(systems, sync_plan, _settings(), dry_run=True)
        assert [result.outcome for result in results] == [Outcome.DRY_RUN]

    def test_personio_attribute_from_the_settings(self) -> None:
        settings = _settings(personio_employee_number_attribute=ATTRIBUTE)
        assert set(load_snapshots(Systems(personio=_personio(ERIKA)), settings, [], TODAY).source.records) == {"00165"}
        # Erika has no employee number in another attribute
        settings = _settings(personio_employee_number_attribute="dynamic_0815")
        assert load_snapshots(Systems(personio=_personio(ERIKA)), settings, [], TODAY).source.records == {}

    @pytest.mark.parametrize(
        ("variable", "field"),
        [
            pytest.param("personio_career_level_attribute", "career_level", id="career level"),
            pytest.param("personio_mobile_phone_attribute", "mobile_phone", id="phone number"),
        ],
    )
    def test_attributes_of_phase_2_from_the_settings(self, variable: str, field: str) -> None:
        assert set(load_snapshots(Systems(personio=_personio(ERIKA)), _settings(), [], TODAY).source.records) == {
            "00165"
        }
        # Erika has no value in another attribute, which is a data problem
        snapshot = load_snapshots(
            Systems(personio=_personio(ERIKA)), _settings(**{variable: "dynamic_0815"}), [], TODAY
        )
        assert snapshot.source.records == {}
        assert [(notice.reason, notice.detail) for notice in snapshot.source.notices] == [
            (NoticeReason.MISSING_FIELD, field)
        ]
