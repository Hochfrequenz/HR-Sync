from datetime import date

import pytest
from pydantic import ValidationError

from hr_sync.model import (
    EmployeeRecord,
    ExclusionReason,
    FieldProblem,
    Notice,
    NoticeReason,
    SnapshotBuilder,
    System,
    display_name_of,
    email_key,
    phone_number_key,
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
            career_level=" Senior Consultant ",
            mobile_phone=" 0151 27 409 444 ",
            office=" Grünwald",
            service_category="Senior Consultant ",
        )
        assert record.employee_number == "00165"
        assert record.first_name == "Erika"
        assert record.last_name == "Musterfrau"
        assert record.display_name == "Erika Musterfrau"
        assert record.email == "erika.musterfrau@example.com"
        assert record.career_level == "Senior Consultant"
        assert record.mobile_phone == "0151 27 409 444"
        assert record.office == "Grünwald"
        assert record.service_category == "Senior Consultant"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_strings_become_none(self, value: str) -> None:
        fields = ("first_name", "last_name", "display_name", "email", "career_level", "mobile_phone", "office")
        record = EmployeeRecord.model_validate(
            {"employee_number": "00165", "service_category": value} | dict.fromkeys(fields, value)
        )
        assert [field for field in (*fields, "service_category") if getattr(record, field) is not None] == []

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
            (ExclusionReason.MISSING_FIELD, True),
            (ExclusionReason.UNMAPPED_VALUE, True),
            (ExclusionReason.INVALID_PHONE_NUMBER, True),
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

    def test_email_owners_include_records_with_data_problems(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", _record("00165"))
        builder.add("user-2", _record("00200", email="Max.Mustermann@Example.com"))
        builder.add("user-3", _record("00200", email="max.m@example.com"))
        builder.add("user-4", _record("00300", email=None))
        builder.add_without_employee_number("user-5", " Admin@Example.com ")
        builder.add_without_employee_number("user-6", "  ")
        builder.add_without_employee_number("user-7")
        assert builder.build().email_owners == {
            "erika.musterfrau@example.com": ("user-1",),
            "max.mustermann@example.com": ("user-2",),
            "max.m@example.com": ("user-3",),
            "admin@example.com": ("user-5",),
        }

    def test_shared_email_has_all_owners(self) -> None:
        builder = SnapshotBuilder(System.DECIDALO)
        builder.add_without_employee_number("1", "shared@example.com")
        builder.add("2", _record("00165", email="Shared@example.com"))
        snapshot = builder.build()
        assert snapshot.email_owners == {"shared@example.com": ("1", "2")}
        # in a target system, a shared e-mail address is not a reason to sort out the record
        assert set(snapshot.records) == {"00165"}

    def test_shared_email_is_a_data_problem_if_required(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        builder.add("1", _record("00165"))
        builder.add("2", _record("00200", email="Erika.Musterfrau@Example.com"))
        builder.add("3", _record("00300", email="max.mustermann@example.com"))
        builder.add_without_employee_number("4", "max.mustermann@example.com")
        builder.add("5", _record("00400", email="someone.else@example.com"))
        snapshot = builder.build()
        assert set(snapshot.records) == {"00400"}
        assert snapshot.excluded == {
            "00165": ExclusionReason.DUPLICATE_EMAIL,
            "00200": ExclusionReason.DUPLICATE_EMAIL,
            "00300": ExclusionReason.DUPLICATE_EMAIL,
        }
        assert [
            (notice.reason, notice.employee_number)
            for notice in snapshot.notices
            if notice.reason == NoticeReason.DUPLICATE_EMAIL
        ] == [
            (NoticeReason.DUPLICATE_EMAIL, "00165"),
            (NoticeReason.DUPLICATE_EMAIL, "00200"),
            (NoticeReason.DUPLICATE_EMAIL, "00300"),
        ]

    def test_system_ids_of_valid_records(self) -> None:
        builder = SnapshotBuilder(System.WECLAPP)
        builder.add("user-1", _record("00165"))
        builder.add("user-2", _record("00200"))
        builder.add("user-3", _record("00200"))
        assert builder.build().system_ids == {"00165": "user-1"}

    def test_deactivated_records(self) -> None:
        builder = SnapshotBuilder(System.DECIDALO)
        builder.add("1", _record("00165"), deactivated=True)
        builder.add("2", _record("00200"), deactivated=True)
        builder.add("3", _record("00200"))
        builder.add("4", _record("00300"))
        snapshot = builder.build()
        # only valid records; the duplicate 00200 is not part of the snapshot
        assert snapshot.deactivated == frozenset({"00165"})
        assert snapshot.notices[0].reason == NoticeReason.DUPLICATE_EMPLOYEE_NUMBER

    def test_problems_in_fields_yield_one_notice_each(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        problems = [
            FieldProblem(reason=ExclusionReason.UNMAPPED_VALUE, field="career_level"),
            FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="office"),
        ]
        builder.add("1", _record("00165"), problems=problems)
        snapshot = builder.build()
        assert snapshot.records == {}
        assert snapshot.system_ids == {}
        assert snapshot.excluded == {"00165": ExclusionReason.UNMAPPED_VALUE}
        assert snapshot.notices == [
            Notice(
                system=System.PERSONIO,
                reason=NoticeReason.UNMAPPED_VALUE,
                system_id="1",
                employee_number="00165",
                detail="career_level",
            ),
            Notice(
                system=System.PERSONIO,
                reason=NoticeReason.MISSING_FIELD,
                system_id="1",
                employee_number="00165",
                detail="office",
            ),
        ]

    @pytest.mark.parametrize(
        ("other", "reason"),
        [
            pytest.param(_record("00165", "other@example.com"), NoticeReason.DUPLICATE_EMPLOYEE_NUMBER, id="number"),
            pytest.param(_record("00200"), NoticeReason.DUPLICATE_EMAIL, id="e-mail address"),
        ],
    )
    def test_records_with_problems_count_for_duplicates(self, other: EmployeeRecord, reason: NoticeReason) -> None:
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        builder.add(
            "1", _record("00165"), problems=[FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="office")]
        )
        builder.add("2", other)
        snapshot = builder.build()
        assert snapshot.records == {}
        # the duplicate wins over the problems in the fields, which are only named once it is solved
        assert [(notice.reason, notice.system_id) for notice in snapshot.notices] == [(reason, "1"), (reason, "2")]

    def test_missing_email_wins_over_problems_in_fields(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        builder.add(
            "1", _record(email=None), problems=[FieldProblem(reason=ExclusionReason.MISSING_FIELD, field="office")]
        )
        snapshot = builder.build()
        assert snapshot.excluded == {"00165": ExclusionReason.MISSING_EMAIL}
        assert [notice.reason for notice in snapshot.notices] == [NoticeReason.MISSING_EMAIL]


class TestEmailKey:
    """
    Tests of the form in which e-mail addresses are compared.
    """

    def test_email_key(self) -> None:
        assert email_key(" Erika.Musterfrau@Example.COM ") == "erika.musterfrau@example.com"


class TestPhoneNumberKey:
    """
    Tests of the form in which phone numbers are compared, which also tells whether a number can be converted.
    """

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            pytest.param("+49 151 23411444", "+4915123411444", id="international"),
            pytest.param("0049 151 23411444", "+4915123411444", id="00 before the country code"),
            pytest.param("0151 27 409 444", "+4915127409444", id="national"),
            pytest.param(" 0151/274-09.444 ", "+4915127409444", id="separators"),
            pytest.param("+43 1 2345678", "+4312345678", id="other country"),
            pytest.param("+49 000 00165", "+4900000165", id="substitute of the test mode"),
            pytest.param("+49 (0) 151 27409444", None, id="parentheses"),
            pytest.param("151 27409444", None, id="without prefix"),
            pytest.param("+49 151 2740+9444", None, id="plus inside"),
            pytest.param("0151 2740 9444 ext", None, id="letters"),
            pytest.param("000 151 27409444", None, id="000 before the country code"),
            pytest.param("01234", None, id="too short"),
            pytest.param("+49 151 2340 9444 1234", None, id="too long"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_phone_number_key(self, number: str, expected: str | None) -> None:
        assert phone_number_key(number) == expected
