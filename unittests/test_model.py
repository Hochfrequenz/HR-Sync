from datetime import date

import pytest
from pydantic import ValidationError

from hr_sync.model import (
    EmployeeRecord,
    ExclusionReason,
    Notice,
    NoticeReason,
    SnapshotBuilder,
    System,
    display_name_of,
)


def _record(employee_number: str = "00165", email: str | None = "erika.musterfrau@example.com") -> EmployeeRecord:
    return EmployeeRecord(
        employee_number=employee_number,
        first_name="Erika",
        last_name="Musterfrau",
        email=email,
        entry_date=date(2026, 10, 1),
    )


class TestEmployeeRecord:
    """
    Tests of the common employee record.
    """

    def test_strings_are_stripped(self) -> None:
        record = EmployeeRecord(
            employee_number=" 00165 ",
            first_name=" Erika ",
            last_name="Musterfrau ",
            display_name=" Erika Musterfrau",
            email=" erika.musterfrau@example.com ",
        )
        assert record.employee_number == "00165"
        assert record.first_name == "Erika"
        assert record.last_name == "Musterfrau"
        assert record.display_name == "Erika Musterfrau"
        assert record.email == "erika.musterfrau@example.com"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_strings_become_none(self, value: str) -> None:
        record = EmployeeRecord(
            employee_number="00165", first_name=value, last_name=value, display_name=value, email=value
        )
        assert record.first_name is None
        assert record.last_name is None
        assert record.display_name is None
        assert record.email is None

    def test_leading_zeros_are_kept(self) -> None:
        assert EmployeeRecord(employee_number="00007").employee_number == "00007"

    @pytest.mark.parametrize("employee_number", ["", "   "])
    def test_employee_number_is_required(self, employee_number: str) -> None:
        with pytest.raises(ValidationError):
            EmployeeRecord(employee_number=employee_number)

    def test_is_frozen(self) -> None:
        record = _record()
        with pytest.raises(ValidationError):
            record.first_name = "Max"  # type: ignore[misc]


class TestDisplayNameOf:
    """
    Tests of the display name used in Decidalo.
    """

    @pytest.mark.parametrize(
        ("first_name", "last_name", "expected"),
        [
            pytest.param("Erika", "Musterfrau", "Erika Musterfrau", id="first and last name"),
            pytest.param(" Erika ", " Musterfrau ", "Erika Musterfrau", id="stripped"),
            pytest.param("Erika", None, "Erika", id="only first name"),
            pytest.param(None, "Musterfrau", "Musterfrau", id="only last name"),
            pytest.param("", " ", None, id="empty"),
            pytest.param(None, None, None, id="none"),
        ],
    )
    def test_display_name_of(self, first_name: str | None, last_name: str | None, expected: str | None) -> None:
        assert display_name_of(first_name, last_name) == expected


class TestExclusionReason:
    """
    Tests of the reasons why loaded employees are not part of a snapshot.
    """

    @pytest.mark.parametrize(
        ("reason", "is_data_problem"),
        [
            (ExclusionReason.EMPLOYMENT_NOT_ACTIVE, False),
            (ExclusionReason.START_DATE_IN_FUTURE, False),
            (ExclusionReason.MISSING_ENTRY_DATE, True),
            (ExclusionReason.MISSING_EMAIL, True),
            (ExclusionReason.DUPLICATE_EMPLOYEE_NUMBER, True),
        ],
    )
    def test_is_data_problem(self, reason: ExclusionReason, is_data_problem: bool) -> None:
        assert reason.is_data_problem is is_data_problem

    @pytest.mark.parametrize("reason", [reason for reason in ExclusionReason if reason.is_data_problem])
    def test_every_data_problem_has_a_notice_reason(self, reason: ExclusionReason) -> None:
        assert NoticeReason(reason.value).value == reason.value


class TestSnapshotBuilder:
    """
    Tests of sorting out the records with data problems.
    """

    def test_valid_record(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", _record())
        snapshot = builder.build()
        assert snapshot.system == System.WECLAPP
        assert snapshot.records == {"00165": _record()}
        assert snapshot.notices == []
        assert snapshot.excluded == {}

    def test_record_without_employee_number(self) -> None:
        builder = SnapshotBuilder(System.DECIDALO)
        builder.add_without_employee_number("42")
        snapshot = builder.build()
        assert snapshot.records == {}
        assert snapshot.notices == [
            Notice(system=System.DECIDALO, reason=NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id="42")
        ]

    def test_duplicate_employee_numbers_are_sorted_out(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", _record("00165"))
        builder.add("user-2", _record("00165"))
        builder.add("user-3", _record("00200"))
        snapshot = builder.build()
        assert set(snapshot.records) == {"00200"}
        assert snapshot.excluded == {"00165": ExclusionReason.DUPLICATE_EMPLOYEE_NUMBER}
        assert snapshot.notices == [
            Notice(
                system=System.WECLAPP,
                reason=NoticeReason.DUPLICATE_EMPLOYEE_NUMBER,
                system_id=system_id,
                employee_number="00165",
            )
            for system_id in ("user-1", "user-2")
        ]

    def test_missing_email_is_a_data_problem_if_required(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        builder.add("1", _record("00165", email=None))
        builder.add("2", _record("00200"))
        snapshot = builder.build()
        assert set(snapshot.records) == {"00200"}
        assert snapshot.excluded == {"00165": ExclusionReason.MISSING_EMAIL}
        assert snapshot.notices == [
            Notice(system=System.PERSONIO, reason=NoticeReason.MISSING_EMAIL, system_id="1", employee_number="00165")
        ]

    def test_missing_email_is_fine_if_not_required(self) -> None:
        builder = SnapshotBuilder(System.DECIDALO)
        builder.add("1", _record(email=None))
        assert set(builder.build().records) == {"00165"}

    def test_not_relevant_employees_yield_no_notice(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO)
        builder.exclude("1", "00165", ExclusionReason.START_DATE_IN_FUTURE)
        builder.exclude("2", None, ExclusionReason.EMPLOYMENT_NOT_ACTIVE)
        snapshot = builder.build()
        assert snapshot.records == {}
        assert snapshot.excluded == {"00165": ExclusionReason.START_DATE_IN_FUTURE}
        assert snapshot.notices == []

    def test_excluded_data_problems_yield_a_notice(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO)
        builder.exclude("1", "00165", ExclusionReason.MISSING_ENTRY_DATE)
        builder.exclude("2", None, ExclusionReason.MISSING_ENTRY_DATE)
        snapshot = builder.build()
        assert snapshot.excluded == {"00165": ExclusionReason.MISSING_ENTRY_DATE}
        assert snapshot.notices == [
            Notice(
                system=System.PERSONIO,
                reason=NoticeReason.MISSING_ENTRY_DATE,
                system_id="1",
                employee_number="00165",
            ),
            Notice(system=System.PERSONIO, reason=NoticeReason.MISSING_ENTRY_DATE, system_id="2"),
        ]

    def test_valid_record_wins_over_an_excluded_one(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO)
        builder.exclude("1", "00165", ExclusionReason.EMPLOYMENT_NOT_ACTIVE)
        builder.add("2", _record("00165"))
        snapshot = builder.build()
        assert set(snapshot.records) == {"00165"}
        assert snapshot.excluded == {}

    def test_incomplete_records(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", _record("00165"), incomplete=True)
        builder.add("user-2", _record("00200"), incomplete=True)
        builder.add("user-3", _record("00200"))
        builder.add("user-4", _record("00300"))
        assert builder.build().incomplete == frozenset({"00165"})

    def test_build_can_be_called_repeatedly(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", _record("00165"))
        builder.add("user-2", _record("00165"))
        builder.add_without_employee_number("user-3")
        assert builder.build() == builder.build()
