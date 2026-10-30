from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import ANY

import pytest
from weclapp_client.models import CustomAttributeDefinition, CustomAttributeType, UserStatus
from weclapp_client.testing import FakeWeclapp

from hr_sync.config import ConfigurationError, ConfigurationProblem
from hr_sync.model import EmployeeRecord, Notice, NoticeReason, System, TargetSystem
from hr_sync.plan import Operation, OperationType, Outcome
from hr_sync.systems.weclapp import (
    LoadedWeclapp,
    WeclappSetup,
    check_setup,
    current_assignment,
    execute_weclapp,
    fetch_weclapp,
    load_weclapp,
    map_weclapp,
    new_employee,
    weclapp_phone_number,
)
from unittests.fakes import WECLAPP_SETUP, weclapp_employee, weclapp_fake, weclapp_user

TODAY = date(2026, 10, 29)
ERIKA = EmployeeRecord(
    employee_number="00165",
    first_name="Erika",
    last_name="Musterfrau",
    display_name="Erika Musterfrau",
    email="erika.musterfrau@example.com",
    entry_date=date(2026, 9, 1),
    career_level="Senior Consultant",
    mobile_phone="0151 27 409 444",
    office="Grünwald",
    weekly_hours=40,
    service_category="Senior Consultant",
    vacation_days=26,
)
"""Erika as Personio has her"""


def _definition(
    definition_id: str,
    attribute_type: str,
    entities: tuple[str, ...] = ("user",),
    values: dict[str, bool | None] | None = None,
) -> CustomAttributeDefinition:
    """a definition with the given selectable values: whether each value id is active"""
    return CustomAttributeDefinition.model_validate(
        {
            "id": definition_id,
            "version": "0",
            "attributeType": attribute_type,
            "entities": list(entities),
            "selectableValues": [{"id": value_id, "active": active} for value_id, active in (values or {}).items()],
        }
    )


STATES = {"9903": True, "9904": True, "9905": None}
"""the federal states of the offices of Hochfrequenz; weclapp may leave ``active`` empty"""


def _loaded(**changes: CustomAttributeDefinition | None) -> LoadedWeclapp:
    """The definitions and profiles of Hochfrequenz, with ``changes`` by id (``_8211`` for ``8211``)."""
    ids = WECLAPP_SETUP.attribute_ids
    definitions: dict[str, CustomAttributeDefinition | None] = {
        "7621": _definition("7621", "STRING"),
        ids.cost_rate: _definition(ids.cost_rate, "DECIMAL"),
        ids.cost_rate_reason: _definition(ids.cost_rate_reason, "STRING"),
        ids.vacation_days: _definition(ids.vacation_days, "INTEGER"),
        ids.state: _definition(ids.state, "LIST", values=STATES | {"9906": False}),
        ids.career_level: _definition(ids.career_level, "STRING"),
    }
    definitions.update({key.removeprefix("_"): value for key, value in changes.items()})
    return LoadedWeclapp(
        definitions=definitions, work_schedule_profile_ids=frozenset(WECLAPP_SETUP.work_schedule_profile_ids.values())
    )


def _create(record: EmployeeRecord = ERIKA) -> Operation:
    return Operation(system=TargetSystem.WECLAPP, type=OperationType.CREATE, record=record)


def _complete(user_id: str, record: EmployeeRecord = ERIKA) -> Operation:
    return Operation(system=TargetSystem.WECLAPP, type=OperationType.COMPLETE, record=record, target_id=user_id)


class TestCheckSetup:
    """
    Tests of checking that the weclapp tenant has the custom attributes and work schedule profiles of HR-Sync.
    """

    def test_tenant_fits(self) -> None:
        check_setup(_loaded(), WECLAPP_SETUP)

    @pytest.mark.parametrize("attribute_type", ["DECIMAL", "INTEGER"])
    def test_numbers_may_be_decimal_or_integer(self, attribute_type: str) -> None:
        check_setup(_loaded(_9795=_definition("9795", attribute_type)), WECLAPP_SETUP)

    @pytest.mark.parametrize(
        "definition",
        [
            pytest.param(None, id="missing"),
            pytest.param(_definition("9902", "STRING"), id="other type"),
            pytest.param(_definition("9902", "LIST", ("party",)), id="not for users"),
        ],
    )
    def test_definition_doesnt_fit(self, definition: CustomAttributeDefinition | None) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_setup(_loaded(_9902=definition), WECLAPP_SETUP)
        assert error_info.value.fields == {"problem": ConfigurationProblem.WECLAPP_SETUP_MISMATCH, "ids": ["9902"]}

    def test_employee_number_names_its_variable(self) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_setup(_loaded(_7621=_definition("7621", "INTEGER")), WECLAPP_SETUP)
        assert error_info.value.variables == ("WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID",)
        assert error_info.value.ids == ("7621",)

    @pytest.mark.parametrize("attribute_id", ["8211", "8217", "9795", "9902", "14175"])
    def test_employee_number_must_not_be_another_attribute(self, attribute_id: str) -> None:
        # otherwise, the type of the other attribute would replace the one of the employee number, or HR-Sync would
        # write two values into one attribute
        setup = replace(WECLAPP_SETUP, employee_number_attribute_id=attribute_id)
        with pytest.raises(ConfigurationError) as error_info:
            check_setup(_loaded(), setup)
        assert error_info.value.problem == ConfigurationProblem.WECLAPP_SETUP_MISMATCH
        assert error_info.value.variables == ("WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID",)
        assert error_info.value.ids == (attribute_id,)

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param({"9903": True, "9904": True}, id="missing"),
            pytest.param(STATES | {"9905": False}, id="inactive"),
        ],
    )
    def test_federal_state_of_every_office(self, values: dict[str, bool | None]) -> None:
        with pytest.raises(ConfigurationError) as error_info:
            check_setup(_loaded(_9902=_definition("9902", "LIST", values=values)), WECLAPP_SETUP)
        assert error_info.value.ids == ("9905",)

    def test_missing_state_is_named_once(self) -> None:
        # the missing definition is the problem, not its values
        with pytest.raises(ConfigurationError) as error_info:
            check_setup(_loaded(_9902=None), WECLAPP_SETUP)
        assert error_info.value.ids == ("9902",)

    def test_missing_work_schedule_profiles(self) -> None:
        loaded = replace(_loaded(), work_schedule_profile_ids=frozenset({"7177", "7633"}))
        with pytest.raises(ConfigurationError) as error_info:
            check_setup(loaded, WECLAPP_SETUP)
        assert "7623" in error_info.value.ids
        assert "7177" not in error_info.value.ids


class TestCurrentAssignment:
    """
    Tests of the work schedule profile that is valid today.
    """

    @pytest.mark.parametrize(
        ("assignments", "expected"),
        [
            pytest.param([("a", date(2026, 9, 1), None)], "a", id="open"),
            pytest.param([("a", TODAY, TODAY)], "a", id="only today"),
            pytest.param(
                [("a", date(2026, 9, 1), date(2026, 10, 1)), ("b", date(2026, 10, 2), None)], "b", id="changed"
            ),
            pytest.param([("a", date(2026, 9, 1), None), ("b", date(2026, 11, 1), None)], "a", id="future"),
            pytest.param([("a", date(2026, 9, 1), date(2026, 10, 1))], None, id="ended"),
            pytest.param([], None, id="none"),
        ],
    )
    def test_current_assignment(self, assignments: list[tuple[str, date, date | None]], expected: str | None) -> None:
        employee = (
            weclapp_employee("2001", *assignments) if assignments else weclapp_employee("2001", ("x", TODAY, None))
        )
        candidates = employee.work_schedule_profile_assignments if assignments else []
        current = current_assignment(candidates, TODAY)
        assert (current.work_schedule_profile_id if current else None) == expected


class TestMapWeclapp:
    """
    Tests of mapping users and employee records onto the common model, with plain data.
    """

    def test_user_with_employee_record(self) -> None:
        snapshot = map_weclapp(
            replace(_loaded(), users=(weclapp_user(),), employees=(weclapp_employee(),)),
            setup=WECLAPP_SETUP,
            today=TODAY,
        )
        assert snapshot.system == System.WECLAPP
        assert snapshot.records == {
            "00165": EmployeeRecord(
                employee_number="00165",
                first_name="Erika",
                last_name="Musterfrau",
                email="erika.musterfrau@example.com",
                entry_date=date(2026, 9, 1),
                career_level="Senior Consultant",
                mobile_phone="+49 151 27409444",
                office="Grünwald",
                weekly_hours=40,
                vacation_days=26,
            )
        }
        assert snapshot.system_ids == {"00165": "2001"}
        assert snapshot.incomplete == frozenset()
        assert snapshot.deactivated == frozenset()
        assert snapshot.notices == []

    def test_user_without_employee_record_is_incomplete(self) -> None:
        snapshot = map_weclapp(replace(_loaded(), users=(weclapp_user(),)), setup=WECLAPP_SETUP, today=TODAY)
        assert snapshot.incomplete == frozenset({"00165"})
        record = snapshot.records["00165"]
        assert (record.entry_date, record.weekly_hours) == (None, None)

    def test_user_without_employee_number(self) -> None:
        snapshot = map_weclapp(
            replace(_loaded(), users=(weclapp_user("2002", None, email="admin@example.com"),)),
            setup=WECLAPP_SETUP,
            today=TODAY,
        )
        assert snapshot.records == {}
        assert snapshot.notices == [
            Notice(system=System.WECLAPP, reason=NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id="2002")
        ]
        # its e-mail address is still in use
        assert snapshot.email_owners == {"admin@example.com": ("2002",)}

    @pytest.mark.parametrize(
        ("status", "deactivated"),
        [("ACTIVE", False), ("DEPARTURE", True), ("NOT_ACTIVE", True), ("SOMETHING_NEW", True)],
    )
    def test_users_that_are_not_active_are_deactivated(self, status: str, deactivated: bool) -> None:
        snapshot = map_weclapp(
            replace(_loaded(), users=(weclapp_user(status=status),), employees=(weclapp_employee(),)),
            setup=WECLAPP_SETUP,
            today=TODAY,
        )
        assert snapshot.deactivated == (frozenset({"00165"}) if deactivated else frozenset())

    def test_status_of_new_users_counts_as_active(self) -> None:
        setup = replace(WECLAPP_SETUP, new_user_status=UserStatus.NOT_ACTIVE)
        snapshot = map_weclapp(
            replace(_loaded(), users=(weclapp_user(status="NOT_ACTIVE"),), employees=(weclapp_employee(),)),
            setup=setup,
            today=TODAY,
        )
        assert snapshot.deactivated == frozenset()

    def test_values_that_cant_be_mapped_back_are_missing(self) -> None:
        user = weclapp_user(career_level="  ", vacation_days="25.5", state_value_id="9999")
        employee = weclapp_employee("2001", ("4711", date(2026, 9, 1), None))
        record = map_weclapp(
            replace(_loaded(), users=(user,), employees=(employee,)), setup=WECLAPP_SETUP, today=TODAY
        ).records["00165"]
        assert (record.career_level, record.vacation_days, record.office, record.weekly_hours) == (
            None,
            None,
            None,
            None,
        )
        # the entry date doesn't depend on the profile
        assert record.entry_date == date(2026, 9, 1)

    def test_user_without_custom_attributes_of_phase_2(self) -> None:
        user = weclapp_user(career_level=None, vacation_days=None, state_value_id=None)
        record = map_weclapp(replace(_loaded(), users=(user,)), setup=WECLAPP_SETUP, today=TODAY).records["00165"]
        assert (record.career_level, record.vacation_days, record.office) == (None, None, None)

    def test_entry_date_and_weekly_hours_after_a_change(self) -> None:
        # from 40 to 30 hours: the entry date stays the start of the first profile
        employee = weclapp_employee(
            "2001", ("7177", date(2026, 9, 1), date(2026, 9, 30)), ("7633", date(2026, 10, 1), None)
        )
        record = map_weclapp(
            replace(_loaded(), users=(weclapp_user(),), employees=(employee,)), setup=WECLAPP_SETUP, today=TODAY
        ).records["00165"]
        assert (record.entry_date, record.weekly_hours) == (date(2026, 9, 1), 30)

    def test_duplicate_employee_numbers(self) -> None:
        users = (weclapp_user("2001"), weclapp_user("2002", email="other@example.com"))
        snapshot = map_weclapp(replace(_loaded(), users=users), setup=WECLAPP_SETUP, today=TODAY)
        assert snapshot.records == {}
        assert [(notice.reason, notice.system_id) for notice in snapshot.notices] == [
            (NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, "2001"),
            (NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, "2002"),
        ]

    def test_employee_records_without_user_are_ignored(self) -> None:
        snapshot = map_weclapp(
            replace(_loaded(), users=(weclapp_user(),), employees=(weclapp_employee(None),)),
            setup=WECLAPP_SETUP,
            today=TODAY,
        )
        assert snapshot.incomplete == frozenset({"00165"})


class TestLoadWeclapp:
    """
    Tests of loading weclapp with the fake of the client, with the event ``sync.load``.
    """

    def test_load(self, events: list[dict[str, object]]) -> None:
        fake, setup = weclapp_fake()
        with fake.client() as client:
            created = client.users.create(new_employee(_create(), setup).user)  # type: ignore[arg-type]
            fake.add_user(email="admin@example.com")
            fake.add_user(
                email="former@example.com",
                status=UserStatus.DEPARTURE,
                custom_attributes={setup.employee_number_attribute_id: "00007"},
            )
            snapshot = load_weclapp(client, setup=setup, today=TODAY)
        assert set(snapshot.records) == {"00165", "00007"}
        assert snapshot.system_ids["00165"] == created.id
        assert snapshot.incomplete == frozenset({"00165", "00007"})
        assert snapshot.deactivated == frozenset({"00007"})
        assert events == [
            {
                "event": "sync.load",
                "level": "INFO",
                "system": "weclapp",
                "users": 3,
                "employees": 0,
                "records": 2,
                "incomplete": 2,
                "deactivated": 1,
                "notices": 1,
                "outcome": "success",
                "duration_ms": ANY,
            }
        ]

    def test_missing_definition_aborts_the_load(self, events: list[dict[str, object]]) -> None:
        fake, setup = weclapp_fake()
        setup = replace(setup, attribute_ids=replace(setup.attribute_ids, state="4711"))
        with fake.client() as client:
            loaded = fetch_weclapp(client, setup)
            with pytest.raises(ConfigurationError) as error_info:
                load_weclapp(client, setup=setup, today=TODAY)
        assert loaded.definitions["4711"] is None
        assert error_info.value.ids == ("4711",)
        assert events[0]["outcome"] == "error"
        assert events[0]["error"] == {
            "type": "ConfigurationError",
            "module": "hr_sync.systems.weclapp",
            "function": "check_setup",
            "line": ANY,
        }


class TestWeclappPhoneNumber:
    """
    Tests of the phone number in the form of the existing weclapp users.
    """

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            pytest.param("0151 46 41 42 22", "+49 151 46414222", id="national"),
            pytest.param("+49 151 46414222", "+49 151 46414222", id="as in weclapp"),
            pytest.param("0049 (151)", None, id="can't be converted"),
            pytest.param("0049-151/464.142.22", "+49 151 46414222", id="separators"),
            pytest.param("030 1234567", "+49 301 234567", id="landline"),
            pytest.param("+43 1 2345678", "+4312345678", id="other country"),
        ],
    )
    def test_weclapp_phone_number(self, number: str, expected: str | None) -> None:
        if expected is None:
            with pytest.raises(ValueError, match="convertible"):
                weclapp_phone_number(number)
        else:
            assert weclapp_phone_number(number) == expected


class TestNewEmployee:
    """
    Tests of the requests that create an employee, as the admin's guide says.
    """

    def test_create(self) -> None:
        new = new_employee(_create(), WECLAPP_SETUP)
        assert new.user_id is None
        assert new.user is not None
        assert new.user.to_payload() == {
            "email": "erika.musterfrau@example.com",
            "status": "ACTIVE",
            "canEditDashboard": False,
            "firstName": "Erika",
            "lastName": "Musterfrau",
            "mobilePhoneNumber": "+49 151 27409444",
            "licenses": ["WECLAPP_PROJECT", "ADDON_HUMAN_RESOURCES"],
            "userRoles": [{"id": "7166"}, {"id": "4329"}],
            "customAttributes": [
                {"attributeDefinitionId": "7621", "stringValue": "00165"},
                {"attributeDefinitionId": "8211", "numberValue": "0"},
                {"attributeDefinitionId": "8217", "stringValue": "Erstanlage (weclapp)"},
                {"attributeDefinitionId": "9795", "numberValue": "26"},
                {"attributeDefinitionId": "9902", "selectedValueId": "9903"},
                {"attributeDefinitionId": "14175", "stringValue": "Senior Consultant"},
            ],
        }
        assert (new.work_schedule_profile.work_schedule_profile_id, new.work_schedule_profile.start_date) == (
            "7177",
            date(2026, 9, 1),
        )

    def test_complete(self) -> None:
        new = new_employee(_complete("2001", ERIKA.model_copy(update={"weekly_hours": 30.0})), WECLAPP_SETUP)
        assert (new.user, new.user_id) == (None, "2001")
        assert new.work_schedule_profile.work_schedule_profile_id == "7633"

    @pytest.mark.parametrize(
        "field", ["weekly_hours", "entry_date", "email", "mobile_phone", "office", "career_level", "vacation_days"]
    )
    def test_missing_field_is_a_programming_error(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            new_employee(_create(ERIKA.model_copy(update={field: None})), WECLAPP_SETUP)

    def test_complete_without_user_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="target_id"):
            new_employee(_complete("2001").model_copy(update={"target_id": None}), WECLAPP_SETUP)


class TestExecuteWeclapp:
    """
    Tests of creating employees with the fake of the client, with the events ``sync.operation``.
    """

    def test_create(self, events: list[dict[str, object]]) -> None:
        fake, setup = weclapp_fake()
        with fake.client() as client:
            results = execute_weclapp(client, [_create()], setup=setup)
        (user,) = fake.users
        (employee,) = fake.employees
        assert [(result.outcome, result.target_id) for result in results] == [(Outcome.SUCCESS, user.id)]
        assert (user.email, user.status, user.licenses) == (ERIKA.email, UserStatus.ACTIVE, list(setup.licenses))
        assert [role.id for role in user.user_roles] == list(setup.user_role_ids)
        assert user.custom_attribute(setup.employee_number_attribute_id).string_value == "00165"  # type: ignore[union-attr]
        assert user.custom_attribute(setup.attribute_ids.cost_rate).number_value == Decimal(0)  # type: ignore[union-attr]
        assert employee.user_id == user.id
        assert [(a.work_schedule_profile_id, a.start_date) for a in employee.work_schedule_profile_assignments] == [
            (setup.work_schedule_profile_ids[40], date(2026, 9, 1))
        ]
        assert [(event["event"], event["outcome"], event["target_id"]) for event in events] == [
            ("sync.operation", "success", user.id)
        ]

    def test_complete(self) -> None:
        fake, setup = weclapp_fake()
        user = fake.add_user(email="erika.musterfrau@example.com")
        with fake.client() as client:
            results = execute_weclapp(client, [_complete(user.id)], setup=setup)
        assert [(result.outcome, result.target_id) for result in results] == [(Outcome.SUCCESS, user.id)]
        assert [employee.user_id for employee in fake.employees] == [user.id]
        assert len(fake.users) == 1

    def test_error_of_one_employee(self, events: list[dict[str, object]]) -> None:
        fake, setup = weclapp_fake()
        fake.fail_next(400, method="POST", path="user")
        max_ = _create(ERIKA.model_copy(update={"employee_number": "00200", "email": "max@example.com"}))
        with fake.client() as client:
            results = execute_weclapp(client, [_create(), max_], setup=setup)
        assert [result.outcome for result in results] == [Outcome.ERROR, Outcome.SUCCESS]
        assert results[0].error == {
            "type": "WeclappValidationError",
            "status_code": 400,
            "problem_type": "injected",
            "module": "hr_sync.systems.weclapp",
            "function": "_send",
            "line": ANY,
        }
        assert results[0].detail is None
        assert [user.email for user in fake.users] == ["max@example.com"]

    @pytest.mark.parametrize("status_code", [401, 429, 503])
    def test_error_of_weclapp_skips_the_remaining_operations(self, status_code: int) -> None:
        fake, setup = weclapp_fake()
        fake.fail_next(status_code, method="POST", times=10)
        max_ = _create(ERIKA.model_copy(update={"employee_number": "00200", "email": "max@example.com"}))
        with fake.client(max_retries=0) as client:
            results = execute_weclapp(client, [_create(), max_], setup=setup)
        assert [result.outcome for result in results] == [Outcome.ERROR, Outcome.SKIPPED]
        assert fake.users == []

    def test_user_without_employee_record(self) -> None:
        fake, setup = weclapp_fake()
        fake.fail_next(400, method="POST", path="employee")
        with fake.client() as client:
            (result,) = execute_weclapp(client, [_create()], setup=setup)
        (user,) = fake.users
        assert (result.outcome, result.target_id, result.detail) == (Outcome.ERROR, user.id, "employee_not_created")
        assert fake.employees == []

    def test_no_personal_data_in_the_events(self, events: list[dict[str, object]]) -> None:
        fake, setup = weclapp_fake()
        fake.fail_next(400, method="POST", path="user")
        with fake.client() as client:
            execute_weclapp(client, [_create()], setup=setup)
        assert [value for value in ("Erika", "Musterfrau", "erika.musterfrau") if value in str(events)] == []

    def test_no_operations(self) -> None:
        with FakeWeclapp().client() as client:
            assert execute_weclapp(client, [], setup=WECLAPP_SETUP) == []

    def test_only_weclapp_operations(self) -> None:
        operation = Operation(system=TargetSystem.DECIDALO, type=OperationType.CREATE, record=ERIKA)
        with FakeWeclapp().client() as client, pytest.raises(ValueError):
            execute_weclapp(client, [operation], setup=WECLAPP_SETUP)

    def test_setup_of_hochfrequenz(self) -> None:
        assert WECLAPP_SETUP == WeclappSetup(employee_number_attribute_id="7621")
        assert WECLAPP_SETUP.state_value_ids == {"Bremen": "9904", "Berlin": "9905", "Grünwald": "9903"}
        assert WECLAPP_SETUP.work_schedule_profile_ids[40] == "7177"
        assert WECLAPP_SETUP.attribute_types[WECLAPP_SETUP.attribute_ids.state] == frozenset({CustomAttributeType.LIST})
