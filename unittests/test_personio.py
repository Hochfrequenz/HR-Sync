import asyncio
from collections.abc import AsyncIterator
from datetime import date
from types import TracebackType
from typing import Literal

import pytest
from personio_client import Employment, Person, PersonioClient
from personio_client.models import PersonCustomAttribute

from hr_sync.model import EmployeeRecord, ExclusionReason, NoticeReason
from hr_sync.systems.personio import (
    LoadedPerson,
    MappedPersons,
    PersonioSource,
    employee_number_of,
    fetch_persons,
    load_personio,
    map_persons,
    relevant_employment,
)

ATTRIBUTE = "company_employee_id"
EARLIER = date(2026, 9, 1)
TODAY = date(2026, 10, 29)
LATER = date(2026, 11, 1)


def _employment(status: str | None = "ACTIVE", start: date | None = EARLIER) -> Employment:
    return Employment(status=status, employment_start_date=start)


def _person(
    person_id: str | None = "1",
    employee_number: str | None = "00165",
    email: str | None = "erika.musterfrau@example.com",
    first_name: str = "Erika",
    last_name: str = "Musterfrau",
) -> Person:
    attributes = [PersonCustomAttribute(id="dynamic_4711", type="string", value="something else")]
    if employee_number is not None:
        attributes.append(PersonCustomAttribute(id=ATTRIBUTE, type="string", value=employee_number))
    return Person(id=person_id, first_name=first_name, last_name=last_name, email=email, custom_attributes=attributes)


def _loaded(person: Person, *employments: Employment) -> LoadedPerson:
    return LoadedPerson(person, employments)


def _map(*persons: LoadedPerson) -> MappedPersons:
    return map_persons(persons, employee_number_attribute=ATTRIBUTE, today=TODAY)


class _FakePersonio:
    """An async Personio client that returns the given persons and employments and records how it is used."""

    def __init__(self, entries: list[tuple[Person, list[Employment]]], fail_for: str | None = None) -> None:
        self._persons = [person for person, _ in entries]
        self._employments = {person.id: employments for person, employments in entries}
        self._fail_for = fail_for
        self.status_filters: list[str | None] = []
        self.requested: list[str] = []
        self.running = 0
        self.max_running = 0
        self.opened = False
        self.closed = False

    async def __aenter__(self) -> "_FakePersonio":
        self.opened = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    async def iter_persons(self, *, status: Literal["ACTIVE", "INACTIVE"] | None = None) -> AsyncIterator[Person]:
        self.status_filters.append(status)
        for person in self._persons:
            yield person

    async def iter_employments(self, person_id: str) -> AsyncIterator[Employment]:
        self.requested.append(person_id)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0)
            if person_id == self._fail_for:
                raise ConnectionError("Personio is not reachable")
            for employment in self._employments[person_id]:
                yield employment
        finally:
            self.running -= 1


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
        candidates = [_employment(status, start) for status, start in employments]
        result = relevant_employment(candidates, TODAY)
        if isinstance(expected, ExclusionReason):
            assert result == expected
        else:
            assert result is candidates[expected]


class TestEmployeeNumberOf:
    """
    Tests of reading the employee number from the custom attribute.
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
        assert employee_number_of(person, ATTRIBUTE) == expected

    def test_attribute_missing(self) -> None:
        assert employee_number_of(_person(employee_number=None), ATTRIBUTE) is None

    def test_no_custom_attributes(self) -> None:
        assert employee_number_of(Person(id="1"), ATTRIBUTE) is None


class TestMapPersons:
    """
    Tests of mapping the loaded persons onto the common model, with plain data.
    """

    def test_relevant_person(self) -> None:
        mapped = _map(_loaded(_person(), _employment()))
        assert mapped.snapshot.records == {
            "00165": EmployeeRecord(
                employee_number="00165",
                first_name="Erika",
                last_name="Musterfrau",
                display_name="Erika Musterfrau",
                email="erika.musterfrau@example.com",
                entry_date=EARLIER,
            )
        }
        assert mapped.snapshot.system_ids == {"00165": "1"}
        assert mapped.snapshot.notices == []
        assert mapped.without_id == 0
        assert mapped.not_relevant == {}

    def test_entry_date_is_the_start_of_the_relevant_employment(self) -> None:
        mapped = _map(_loaded(_person(), _employment("INACTIVE", date(2020, 1, 1)), _employment("ACTIVE", TODAY)))
        assert mapped.snapshot.records["00165"].entry_date == TODAY

    def test_not_relevant_persons_yield_no_notices(self) -> None:
        mapped = _map(
            _loaded(_person("1", "00300", email=None), _employment("ONBOARDING", LATER)),
            _loaded(_person("2", None, email=None), _employment("ACTIVE", LATER)),
            _loaded(_person("3", "00400"), _employment("ACTIVE", LATER)),
            _loaded(_person("4", "00500"), _employment("LEAVE", EARLIER)),
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
            _loaded(_person("1", "00100"), _employment("ACTIVE", None)),
            _loaded(_person("2", None, email="max.mustermann@example.com"), _employment()),
            _loaded(_person("3", "00300", email=None), _employment()),
            _loaded(_person("4", "00400", email="shared@example.com"), _employment()),
            _loaded(_person("5", "00500", email="Shared@example.com"), _employment()),
            _loaded(_person("6", "00600", email="valid@example.com"), _employment()),
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
        mapped = _map(_loaded(_person(None, "00165")), _loaded(_person("2", "00200"), _employment()))
        assert set(mapped.snapshot.records) == {"00200"}
        assert mapped.without_id == 1


class TestFetchPersons:
    """
    Tests of loading the persons with the (async) Personio client.
    """

    def test_loads_the_active_persons_with_their_employments(self) -> None:
        erika, max_ = _person("1", "00165"), _person("2", "00200")
        client = _FakePersonio([(erika, [_employment()]), (max_, [_employment("INACTIVE"), _employment()])])
        assert fetch_persons(client) == [
            _loaded(erika, _employment()),
            _loaded(max_, _employment("INACTIVE"), _employment()),
        ]
        assert client.status_filters == ["ACTIVE"]
        assert client.opened
        assert client.closed

    def test_requests_one_after_the_other(self) -> None:
        client = _FakePersonio([(_person(str(i), f"00{i}00"), [_employment()]) for i in range(1, 6)])
        fetch_persons(client)
        assert client.requested == ["1", "2", "3", "4", "5"]
        assert client.max_running == 1

    def test_person_without_id_gets_no_request(self) -> None:
        without_id = _person(None, "00165")
        client = _FakePersonio([(without_id, []), (_person("2", "00200"), [_employment()])])
        persons = fetch_persons(client)
        assert persons[0] == _loaded(without_id)
        assert client.requested == ["2"]

    def test_client_is_closed_after_an_error(self) -> None:
        client = _FakePersonio([(_person("1", "00100"), [_employment()])], fail_for="1")
        with pytest.raises(ConnectionError):
            fetch_persons(client)
        assert client.closed


class TestLoadPersonio:
    """
    Tests of loading and mapping together, with the event ``sync.load``.
    """

    def test_event(self, events: list[dict[str, object]]) -> None:
        client = _FakePersonio(
            [
                (_person("1", "00100"), [_employment()]),
                (_person("2", "00200", email=None), [_employment()]),
                (_person("3", "00300"), [_employment("ONBOARDING", LATER)]),
                (_person("4", "00400"), [_employment("ACTIVE", LATER)]),
                (_person("5", "00500"), [_employment("LEAVE", EARLIER)]),
                (_person(None, "00600"), []),
            ]
        )
        snapshot = load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY)
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
        client = _FakePersonio([(_person("1", "00100"), [_employment()]), (_person("2", "00200"), [])], fail_for="2")
        with pytest.raises(ConnectionError):
            load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY)
        assert events[0]["outcome"] == "error"
        assert events[0]["error"] == {"type": "ConnectionError"}

    def test_personio_client_is_a_source(self) -> None:
        # mypy checks that the real client fits the protocol the adapter expects
        source: PersonioSource = PersonioClient(client_id="id", client_secret="secret")
        assert source is not None
