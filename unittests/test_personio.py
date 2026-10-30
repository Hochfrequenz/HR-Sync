from datetime import date
from unittest.mock import ANY

import pytest
from personio_client import Employment, Person, PersonioClient
from personio_client.models import PersonCustomAttribute

from hr_sync.model import EmployeeRecord, ExclusionReason, FieldProblem, NoticeReason
from hr_sync.systems.personio import (
    LoadedPerson,
    MappedPersons,
    PersonioSource,
    attribute_value,
    fetch_persons,
    load_personio,
    map_person,
    map_persons,
    relevant_employment,
)
from unittests.fakes import ATTRIBUTE, ATTRIBUTES, PHONE, FakePersonio, personio_employment, personio_person

EARLIER = date(2026, 9, 1)
TODAY = date(2026, 10, 29)
LATER = date(2026, 11, 1)


def _loaded(person: Person, *employments: Employment) -> LoadedPerson:
    return LoadedPerson(person, employments)


def _map(*persons: LoadedPerson) -> MappedPersons:
    return map_persons(persons, attributes=ATTRIBUTES, today=TODAY)


def _map_person(person: Person, employment: Employment | None = None) -> tuple[EmployeeRecord, list[FieldProblem]]:
    return map_person(person, employment or personio_employment(), attributes=ATTRIBUTES, employee_number="00165")


class TestRelevantEmployment:
    """
    Tests of the rule which persons are relevant for the sync.
    """

    @pytest.mark.parametrize(
        ("employments", "expected"),
        [
            pytest.param([("ACTIVE", EARLIER)], 0, id="active, started earlier"),
            pytest.param([("ACTIVE", TODAY)], 0, id="active, starts today"),
            pytest.param([("active", EARLIER)], 0, id="status in lower case"),
            pytest.param([("ACTIVE", LATER)], ExclusionReason.START_DATE_IN_FUTURE, id="active, starts tomorrow"),
            pytest.param([("ONBOARDING", EARLIER)], ExclusionReason.EMPLOYMENT_NOT_ACTIVE, id="onboarding"),
            pytest.param([("LEAVE", EARLIER)], ExclusionReason.EMPLOYMENT_NOT_ACTIVE, id="on leave"),
            pytest.param([("INACTIVE", EARLIER)], ExclusionReason.EMPLOYMENT_NOT_ACTIVE, id="inactive"),
            pytest.param([(None, EARLIER)], ExclusionReason.EMPLOYMENT_NOT_ACTIVE, id="without status"),
            pytest.param([], ExclusionReason.EMPLOYMENT_NOT_ACTIVE, id="no employment"),
            pytest.param([("ACTIVE", None)], ExclusionReason.MISSING_ENTRY_DATE, id="active without start"),
            pytest.param(
                [("ACTIVE", None), ("ACTIVE", EARLIER)], 1, id="without start, but another relevant employment"
            ),
            pytest.param(
                [("ACTIVE", None), ("ACTIVE", LATER)],
                ExclusionReason.MISSING_ENTRY_DATE,
                id="without start next to a future employment",
            ),
            pytest.param([("INACTIVE", date(2020, 1, 1)), ("ACTIVE", TODAY)], 1, id="re-entry"),
            pytest.param([("ACTIVE", EARLIER), ("ACTIVE", TODAY)], 1, id="the latest start wins"),
            pytest.param([("ACTIVE", TODAY), ("ACTIVE", LATER)], 0, id="a future employment is ignored"),
        ],
    )
    def test_relevant_employment(
        self, employments: list[tuple[str | None, date | None]], expected: int | ExclusionReason
    ) -> None:
        candidates = [personio_employment(status, start) for status, start in employments]
        result = relevant_employment(candidates, TODAY)
        if isinstance(expected, ExclusionReason):
            assert result == expected
        else:
            assert result is candidates[expected]


class TestAttributeValue:
    """
    Tests of reading a custom attribute, e.g. the employee number.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("00165", "00165", id="string with leading zeros"),
            pytest.param(" 00165 ", "00165", id="stripped"),
            pytest.param("", None, id="empty"),
            pytest.param("  ", None, id="blank"),
            pytest.param(165, "165", id="number"),
            pytest.param(True, None, id="boolean"),
            pytest.param(None, None, id="null"),
        ],
    )
    def test_value(self, value: str | int | bool | None, expected: str | None) -> None:
        person = Person(id="1", custom_attributes=[PersonCustomAttribute(id=ATTRIBUTE, value=value)])
        assert attribute_value(person, ATTRIBUTE) == expected

    def test_attribute_missing(self) -> None:
        assert attribute_value(personio_person(employee_number=None), ATTRIBUTE) is None

    def test_no_custom_attributes(self) -> None:
        assert attribute_value(Person(id="1"), ATTRIBUTE) is None


class TestMapPersons:
    """
    Tests of mapping the loaded persons onto the common model, with plain data.
    """

    def test_relevant_person(self) -> None:
        mapped = _map(_loaded(personio_person(), personio_employment()))
        assert mapped.snapshot.records == {
            "00165": EmployeeRecord(
                employee_number="00165",
                first_name="Erika",
                last_name="Musterfrau",
                display_name="Erika Musterfrau",
                email="erika.musterfrau@example.com",
                entry_date=EARLIER,
                career_level="Senior Consultant",
                mobile_phone=PHONE,
                office="Grünwald",
                weekly_hours=40,
                service_category="Senior Consultant",
                vacation_days=26,
            )
        }
        assert mapped.snapshot.system_ids == {"00165": "1"}
        assert mapped.snapshot.notices == []
        assert mapped.without_id == 0
        assert mapped.not_relevant == {}

    def test_entry_date_is_the_start_of_the_relevant_employment(self) -> None:
        mapped = _map(
            _loaded(
                personio_person(),
                personio_employment("INACTIVE", date(2020, 1, 1)),
                personio_employment("ACTIVE", TODAY),
            )
        )
        assert mapped.snapshot.records["00165"].entry_date == TODAY

    def test_not_relevant_persons_yield_no_notices(self) -> None:
        mapped = _map(
            _loaded(personio_person("1", "00300", email=None), personio_employment("ONBOARDING", LATER)),
            _loaded(personio_person("2", None, email=None), personio_employment("ACTIVE", LATER)),
            _loaded(personio_person("3", "00400"), personio_employment("ACTIVE", LATER)),
            _loaded(personio_person("4", "00500"), personio_employment("LEAVE", EARLIER)),
        )
        assert mapped.snapshot.records == {}
        assert mapped.snapshot.notices == []
        assert mapped.snapshot.excluded == {
            "00300": ExclusionReason.EMPLOYMENT_NOT_ACTIVE,
            "00400": ExclusionReason.START_DATE_IN_FUTURE,
            "00500": ExclusionReason.EMPLOYMENT_NOT_ACTIVE,
        }
        assert mapped.not_relevant == {
            ExclusionReason.EMPLOYMENT_NOT_ACTIVE: 2,
            ExclusionReason.START_DATE_IN_FUTURE: 2,
        }

    def test_data_problems_of_relevant_persons(self) -> None:
        mapped = _map(
            _loaded(personio_person("1", "00100"), personio_employment("ACTIVE", None)),
            _loaded(personio_person("2", None, email="max.mustermann@example.com"), personio_employment()),
            _loaded(personio_person("3", "00300", email=None), personio_employment()),
            _loaded(personio_person("4", "00400", email="shared@example.com"), personio_employment()),
            _loaded(personio_person("5", "00500", email="Shared@example.com"), personio_employment()),
            _loaded(personio_person("6", "00600", email="valid@example.com"), personio_employment()),
        )
        assert set(mapped.snapshot.records) == {"00600"}
        assert sorted((notice.reason, notice.system_id) for notice in mapped.snapshot.notices) == [
            (NoticeReason.DUPLICATE_EMAIL, "4"),
            (NoticeReason.DUPLICATE_EMAIL, "5"),
            (NoticeReason.MISSING_EMAIL, "3"),
            (NoticeReason.MISSING_EMPLOYEE_NUMBER, "2"),
            (NoticeReason.MISSING_ENTRY_DATE, "1"),
        ]
        # a data problem is not "not relevant"
        assert mapped.not_relevant == {}

    def test_person_without_id_is_skipped(self) -> None:
        mapped = _map(
            _loaded(personio_person(None, "00165")), _loaded(personio_person("2", "00200"), personio_employment())
        )
        assert set(mapped.snapshot.records) == {"00200"}
        assert mapped.without_id == 1


class TestMapPerson:
    """
    Tests of the fields of phase 2 and of the mandatory fields, with plain data.
    """

    @pytest.mark.parametrize(
        ("career_level", "service_category", "vacation_days"),
        [
            pytest.param("Consultant", "Consultant", 25, id="consultant"),
            pytest.param("Senior Consultant", "Senior Consultant", 26, id="senior consultant"),
            pytest.param("Principal Consultant", "Manging Consultant", 30, id="principal consultant"),
            pytest.param("Senior Principal Consultant", "Associate Partner", 30, id="senior principal consultant"),
        ],
    )
    def test_career_level(self, career_level: str, service_category: str, vacation_days: int) -> None:
        record, problems = _map_person(personio_person(career_level=f" {career_level} "))
        assert record.career_level == career_level
        assert record.service_category == service_category
        assert record.vacation_days == vacation_days
        assert problems == []

    @pytest.mark.parametrize(
        ("office_id", "office"),
        [
            pytest.param("906847", "Bremen", id="Bremen"),
            pytest.param("906855", "Berlin", id="Berlin"),
            pytest.param("906856", "Grünwald", id="Grünwald"),
        ],
    )
    def test_office(self, office_id: str, office: str) -> None:
        record, problems = _map_person(personio_person(), personio_employment(office_id=office_id))
        assert record.office == office
        assert problems == []

    def test_weekly_hours_of_the_relevant_employment(self) -> None:
        record, problems = _map_person(personio_person(), personio_employment(weekly_hours=30.0))
        assert record.weekly_hours == 30
        assert problems == []

    @pytest.mark.parametrize("mobile_phone", ["+49 151 27409444", "0049 151 27409444", "0151/274-09.444"])
    def test_phone_number_stays_as_in_personio(self, mobile_phone: str) -> None:
        record, problems = _map_person(personio_person(mobile_phone=mobile_phone))
        assert record.mobile_phone == mobile_phone
        assert problems == []

    @pytest.mark.parametrize(
        ("person", "employment", "reason", "field"),
        [
            pytest.param(
                personio_person(first_name=" "), None, ExclusionReason.MISSING_FIELD, "first_name", id="no first name"
            ),
            pytest.param(
                personio_person(last_name=None), None, ExclusionReason.MISSING_FIELD, "last_name", id="no last name"
            ),
            pytest.param(
                personio_person(first_name="E" * 51),
                None,
                ExclusionReason.VALUE_TOO_LONG,
                "first_name",
                id="first name too long for weclapp",
            ),
            pytest.param(
                personio_person(last_name="M" * 51),
                None,
                ExclusionReason.VALUE_TOO_LONG,
                "last_name",
                id="last name too long for weclapp",
            ),
            pytest.param(
                personio_person(career_level=None),
                None,
                ExclusionReason.MISSING_FIELD,
                "career_level",
                id="no career level",
            ),
            pytest.param(
                personio_person(career_level="Partner"),
                None,
                ExclusionReason.UNMAPPED_VALUE,
                "career_level",
                id="unknown career level",
            ),
            pytest.param(
                personio_person(career_level="senior consultant"),
                None,
                ExclusionReason.UNMAPPED_VALUE,
                "career_level",
                id="career level in other case",
            ),
            pytest.param(
                personio_person(mobile_phone=""), None, ExclusionReason.MISSING_FIELD, "mobile_phone", id="no phone"
            ),
            pytest.param(
                personio_person(mobile_phone="+49 (0) 151 27409444"),
                None,
                ExclusionReason.INVALID_PHONE_NUMBER,
                "mobile_phone",
                id="phone number with parentheses",
            ),
            pytest.param(
                personio_person(mobile_phone="151 27409444"),
                None,
                ExclusionReason.INVALID_PHONE_NUMBER,
                "mobile_phone",
                id="phone number without prefix",
            ),
            pytest.param(
                personio_person(),
                personio_employment(office_id=None),
                ExclusionReason.MISSING_FIELD,
                "office",
                id="no office",
            ),
            pytest.param(
                personio_person(),
                personio_employment(office_id="4711"),
                ExclusionReason.UNMAPPED_VALUE,
                "office",
                id="unknown office",
            ),
            pytest.param(
                personio_person(),
                personio_employment(weekly_hours=None),
                ExclusionReason.MISSING_FIELD,
                "weekly_hours",
                id="no weekly hours",
            ),
            pytest.param(
                personio_person(),
                personio_employment(weekly_hours=38),
                ExclusionReason.UNMAPPED_VALUE,
                "weekly_hours",
                id="unknown weekly hours",
            ),
            pytest.param(
                personio_person(),
                personio_employment(weekly_hours=37.5),
                ExclusionReason.UNMAPPED_VALUE,
                "weekly_hours",
                id="fraction of an hour",
            ),
        ],
    )
    def test_problem(self, person: Person, employment: Employment | None, reason: ExclusionReason, field: str) -> None:
        _, problems = _map_person(person, employment or personio_employment())
        assert [(problem.reason, problem.field) for problem in problems] == [(reason, field)]

    def test_all_problems_in_the_order_of_the_fields(self) -> None:
        _, problems = _map_person(
            personio_person(first_name=None, last_name=None, career_level="Partner", mobile_phone="n/a"),
            personio_employment(office_id=None, weekly_hours=None),
        )
        assert problems == [
            FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="first_name"),
            FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="last_name"),
            FieldProblem(reason=ExclusionReason.UNMAPPED_VALUE, field="career_level"),
            FieldProblem(reason=ExclusionReason.INVALID_PHONE_NUMBER, field="mobile_phone"),
            FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="office"),
            FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="weekly_hours"),
        ]

    def test_person_with_problems_is_not_part_of_the_snapshot(self) -> None:
        mapped = _map(
            _loaded(personio_person("1", "00100", career_level=None, mobile_phone="n/a"), personio_employment()),
            _loaded(personio_person("2", "00200", email="valid@example.com"), personio_employment()),
        )
        assert set(mapped.snapshot.records) == {"00200"}
        assert mapped.snapshot.excluded == {"00100": ExclusionReason.MISSING_FIELD}
        assert [
            (notice.reason, notice.system_id, notice.employee_number, notice.detail)
            for notice in mapped.snapshot.notices
        ] == [
            (NoticeReason.MISSING_FIELD, "1", "00100", "career_level"),
            (NoticeReason.INVALID_PHONE_NUMBER, "1", "00100", "mobile_phone"),
        ]

    def test_person_with_problems_still_counts_for_duplicates(self) -> None:
        mapped = _map(
            # without career level, so not created, but its e-mail address and employee number are still in use
            _loaded(
                personio_person("1", "00100", email="shared@example.com", career_level=None), personio_employment()
            ),
            _loaded(personio_person("2", "00200", email="shared@example.com"), personio_employment()),
            _loaded(personio_person("3", "00300", email="one@example.com", career_level=None), personio_employment()),
            _loaded(personio_person("4", "00300", email="other@example.com"), personio_employment()),
        )
        assert mapped.snapshot.records == {}
        assert sorted((notice.reason, notice.system_id) for notice in mapped.snapshot.notices) == [
            (NoticeReason.DUPLICATE_EMAIL, "1"),
            (NoticeReason.DUPLICATE_EMAIL, "2"),
            (NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, "3"),
            (NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, "4"),
        ]


class TestFetchPersons:
    """
    Tests of loading the persons with the (async) Personio client.
    """

    def test_loads_the_active_persons_with_their_employments(self) -> None:
        erika, max_ = personio_person("1", "00165"), personio_person("2", "00200")
        client = FakePersonio(
            [(erika, [personio_employment()]), (max_, [personio_employment("INACTIVE"), personio_employment()])]
        )
        assert fetch_persons(client) == [
            _loaded(erika, personio_employment()),
            _loaded(max_, personio_employment("INACTIVE"), personio_employment()),
        ]
        assert client.status_filters == ["ACTIVE"]
        assert client.opened
        assert client.closed

    def test_requests_one_after_the_other(self) -> None:
        client = FakePersonio([(personio_person(str(i), f"00{i}00"), [personio_employment()]) for i in range(1, 6)])
        fetch_persons(client)
        assert client.requested == ["1", "2", "3", "4", "5"]
        assert client.max_running == 1

    def test_person_without_id_gets_no_request(self) -> None:
        without_id = personio_person(None, "00165")
        client = FakePersonio([(without_id, []), (personio_person("2", "00200"), [personio_employment()])])
        persons = fetch_persons(client)
        assert persons[0] == _loaded(without_id)
        assert client.requested == ["2"]

    def test_client_is_closed_after_an_error(self) -> None:
        client = FakePersonio([(personio_person("1", "00100"), [personio_employment()])], fail_for="1")
        with pytest.raises(ConnectionError):
            fetch_persons(client)
        assert client.closed


class TestLoadPersonio:
    """
    Tests of loading and mapping together, with the event ``sync.load``.
    """

    def test_event(self, events: list[dict[str, object]]) -> None:
        client = FakePersonio(
            [
                (personio_person("1", "00100"), [personio_employment()]),
                (personio_person("2", "00200", email=None), [personio_employment()]),
                (personio_person("3", "00300"), [personio_employment("ONBOARDING", LATER)]),
                (personio_person("4", "00400"), [personio_employment("ACTIVE", LATER)]),
                (personio_person("5", "00500"), [personio_employment("LEAVE", EARLIER)]),
                (personio_person(None, "00600"), []),
            ]
        )
        snapshot = load_personio(client, attributes=ATTRIBUTES, today=TODAY)
        assert set(snapshot.records) == {"00100"}
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "sync.load"
        assert event["system"] == "personio"
        assert event["outcome"] == "success"
        assert event["persons"] == 6
        assert event["without_id"] == 1
        assert event["records"] == 1
        assert event["notices"] == 1
        assert event["not_relevant"] == {"employment_not_active": 2, "start_date_in_future": 1}

    def test_error_aborts_the_load(self, events: list[dict[str, object]]) -> None:
        client = FakePersonio(
            [(personio_person("1", "00100"), [personio_employment()]), (personio_person("2", "00200"), [])],
            fail_for="2",
        )
        with pytest.raises(ConnectionError):
            load_personio(client, attributes=ATTRIBUTES, today=TODAY)
        assert events[0]["outcome"] == "error"
        assert events[0]["error"] == {
            "type": "ConnectionError",
            "module": "hr_sync.systems.personio",
            "function": "_fetch_persons",
            "line": ANY,
        }

    def test_personio_client_is_a_source(self) -> None:
        # mypy checks that the real client fits the protocol the adapter expects
        source: PersonioSource = PersonioClient(client_id="id", client_secret="secret")
        assert source is not None
