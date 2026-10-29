import asyncio
from collections.abc import AsyncIterator
from datetime import date
from typing import Literal

import pytest
from personio_client import Employment, Person, PersonioClient
from personio_client.models import PersonCustomAttribute

from hr_sync.model import EmployeeRecord, ExclusionReason, NoticeReason
from hr_sync.systems.personio import PersonioSource, employee_number_of, load_personio, relevant_employment

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


class _FakePersonio:
    """A Personio client that returns the given persons and employments and records how it is used."""

    def __init__(
        self,
        entries: list[tuple[Person, list[Employment]]],
        fail_for: str | None = None,
        slow_for: str | None = None,
    ) -> None:
        self._persons = [person for person, _ in entries]
        self._employments = {person.id: employments for person, employments in entries}
        self._fail_for = fail_for
        self._slow_for = slow_for
        self.status_filters: list[str | None] = []
        self.running = 0
        self.max_running = 0
        self.cancelled: list[str] = []

    async def iter_persons(self, *, status: Literal["ACTIVE", "INACTIVE"] | None = None) -> AsyncIterator[Person]:
        self.status_filters.append(status)
        for person in self._persons:
            yield person

    async def iter_employments(self, person_id: str) -> AsyncIterator[Employment]:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0)
            if person_id == self._fail_for:
                raise ConnectionError("Personio is not reachable")
            if person_id == self._slow_for:
                await asyncio.sleep(60)
            for employment in self._employments[person_id]:
                yield employment
        except asyncio.CancelledError:
            self.cancelled.append(person_id)
            raise
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


class TestLoadPersonio:
    """
    Tests of loading and mapping the persons of Personio.
    """

    def test_relevant_person(self) -> None:
        client = _FakePersonio([(_person(), [_employment()])])
        snapshot = asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert snapshot.records == {
            "00165": EmployeeRecord(
                employee_number="00165",
                first_name="Erika",
                last_name="Musterfrau",
                display_name="Erika Musterfrau",
                email="erika.musterfrau@example.com",
                entry_date=EARLIER,
            )
        }
        assert snapshot.system_ids == {"00165": "1"}
        assert snapshot.notices == []
        assert client.status_filters == ["ACTIVE"]

    def test_entry_date_is_the_start_of_the_relevant_employment(self) -> None:
        client = _FakePersonio([(_person(), [_employment("INACTIVE", date(2020, 1, 1)), _employment("ACTIVE", TODAY)])])
        snapshot = asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert snapshot.records["00165"].entry_date == TODAY

    def test_not_relevant_persons_yield_no_notices(self) -> None:
        client = _FakePersonio(
            [
                (_person("1", "00300", email=None), [_employment("ONBOARDING", LATER)]),
                (_person("2", None, email=None), [_employment("ACTIVE", LATER)]),
                (_person("3", "00400"), [_employment("ACTIVE", LATER)]),
            ]
        )
        snapshot = asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert snapshot.records == {}
        assert snapshot.notices == []
        assert snapshot.excluded == {
            "00300": ExclusionReason.EMPLOYMENT_NOT_ACTIVE,
            "00400": ExclusionReason.START_DATE_IN_FUTURE,
        }

    def test_data_problems_of_relevant_persons(self) -> None:
        client = _FakePersonio(
            [
                (_person("1", "00100"), [_employment("ACTIVE", None)]),
                (_person("2", None, email="max.mustermann@example.com"), [_employment()]),
                (_person("3", "00300", email=None), [_employment()]),
                (_person("4", "00400", email="shared@example.com"), [_employment()]),
                (_person("5", "00500", email="Shared@example.com"), [_employment()]),
                (_person("6", "00600", email="valid@example.com"), [_employment()]),
            ]
        )
        snapshot = asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert set(snapshot.records) == {"00600"}
        assert sorted((notice.reason, notice.system_id) for notice in snapshot.notices) == [
            (NoticeReason.DUPLICATE_EMAIL, "4"),
            (NoticeReason.DUPLICATE_EMAIL, "5"),
            (NoticeReason.MISSING_EMAIL, "3"),
            (NoticeReason.MISSING_EMPLOYEE_NUMBER, "2"),
            (NoticeReason.MISSING_ENTRY_DATE, "1"),
        ]

    def test_person_without_id_is_skipped(self, events: list[dict[str, object]]) -> None:
        client = _FakePersonio([(_person(None, "00165"), []), (_person("2", "00200"), [_employment()])])
        snapshot = asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert set(snapshot.records) == {"00200"}
        assert events[-1]["without_id"] == 1

    def test_event(self, events: list[dict[str, object]]) -> None:
        client = _FakePersonio(
            [
                (_person("1", "00100"), [_employment()]),
                (_person("2", "00200", email=None), [_employment()]),
                (_person("3", "00300"), [_employment("ONBOARDING", LATER)]),
                (_person("4", "00400"), [_employment("ACTIVE", LATER)]),
                (_person("5", "00500"), [_employment("LEAVE", EARLIER)]),
            ]
        )
        asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "sync.load"
        assert event["system"] == "personio"
        assert event["outcome"] == "success"
        assert event["persons"] == 5
        assert event["without_id"] == 0
        assert event["records"] == 1
        assert event["notices"] == 1
        assert event["not_relevant"] == {"employment_not_active": 2, "start_date_in_future": 1}

    def test_concurrency_is_limited(self) -> None:
        client = _FakePersonio([(_person(str(i), f"00{i}00"), [_employment()]) for i in range(1, 7)])
        asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY, concurrency=2))
        assert client.max_running == 2

    def test_error_aborts_the_load(self, events: list[dict[str, object]]) -> None:
        client = _FakePersonio([(_person("1", "00100"), [_employment()]), (_person("2", "00200"), [])], fail_for="2")
        with pytest.raises(ConnectionError):
            asyncio.run(load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY))
        assert events[0]["outcome"] == "error"
        assert events[0]["error"] == {"type": "ConnectionError"}

    def test_error_cancels_the_other_requests(self) -> None:
        client = _FakePersonio(
            [(_person("1", "00100"), [_employment()]), (_person("2", "00200"), [_employment()])],
            fail_for="1",
            slow_for="2",
        )

        async def load_and_check() -> list[str]:
            with pytest.raises(ConnectionError):
                await load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY)
            # checked right after the error, before asyncio.run() cancels whatever is still running
            return list(client.cancelled)

        assert asyncio.run(load_and_check()) == ["2"]
        assert client.running == 0

    @pytest.mark.parametrize("concurrency", [0, -1])
    def test_concurrency_must_be_positive(self, concurrency: int) -> None:
        client = _FakePersonio([(_person(), [_employment()])])
        with pytest.raises(ValueError, match="concurrency"):
            asyncio.run(
                load_personio(client, employee_number_attribute=ATTRIBUTE, today=TODAY, concurrency=concurrency)
            )
        assert client.status_filters == []

    def test_personio_client_is_a_source(self) -> None:
        # mypy checks that the real client fits the protocol the loader expects
        source: PersonioSource = PersonioClient(client_id="id", client_secret="secret")
        assert source is not None
