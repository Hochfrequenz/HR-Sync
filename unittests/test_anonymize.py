from datetime import date

from hr_sync.anonymize import KEPT_FIELDS, SUBSTITUTES, anonymize, anonymize_record
from hr_sync.model import EmployeeRecord, ExclusionReason, SnapshotBuilder, System, phone_number_key

ERIKA = EmployeeRecord(
    employee_number="00165",
    first_name="Erika",
    last_name="Musterfrau",
    display_name="Erika Musterfrau",
    email="erika.musterfrau@example.com",
    entry_date=date(2026, 10, 1),
    career_level="Senior Consultant",
    mobile_phone="0151 27 409 444",
    office="Grünwald",
    weekly_hours=40,
    service_category="Senior Consultant",
    vacation_days=26,
)
REAL_VALUES = (
    "Erika",
    "Musterfrau",
    "erika.musterfrau",
    "Max",
    "Mustermann",
    "max.mustermann",
    "admin",
    "0151 27 409 444",
)
"""the personal data of the test data, which must not be left after anonymizing"""


class TestAnonymizeRecord:
    """
    Tests of the substitutes for the personal data of an employee.
    """

    def test_substitutes(self) -> None:
        assert anonymize_record(ERIKA) == EmployeeRecord(
            employee_number="00165",
            first_name="Test",
            last_name="Person-00165",
            display_name="Test Person-00165",
            email="test-00165@example.com",
            entry_date=date(2026, 10, 1),
            career_level="Senior Consultant",
            mobile_phone="+49 000 00165",
            office="Grünwald",
            weekly_hours=40,
            service_category="Senior Consultant",
            vacation_days=26,
        )

    def test_substitute_phone_number_is_valid(self) -> None:
        # a substitute must pass the same checks as a real number, e.g. when it's compared
        assert phone_number_key(SUBSTITUTES["mobile_phone"]("00165")) == "+4900000165"

    def test_every_field_has_a_rule(self) -> None:
        # a new field needs a decision: substitute it or keep it real
        assert KEPT_FIELDS | SUBSTITUTES.keys() == EmployeeRecord.model_fields.keys()
        assert not KEPT_FIELDS & SUBSTITUTES.keys()

    def test_substitutes_only_depend_on_the_employee_number(self) -> None:
        other = EmployeeRecord(
            employee_number="00165", first_name="Max", email="max@example.com", mobile_phone="0170 1234567"
        )
        substituted = set(SUBSTITUTES)
        assert anonymize_record(other).model_dump(include=substituted) == anonymize_record(ERIKA).model_dump(
            include=substituted
        )


class TestAnonymize:
    """
    Tests of anonymizing the snapshot of Personio.
    """

    def test_snapshot(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO, require_email=True)
        builder.add("1", ERIKA)
        builder.add("2", EmployeeRecord(employee_number="00200", first_name="Max", email="max.mustermann@example.com"))
        # shares the address of the record without employee number, so both are excluded
        builder.add("3", EmployeeRecord(employee_number="00300", last_name="Mustermann", email="Admin@example.com"))
        builder.add_without_employee_number("4", "admin@example.com")
        builder.exclude("5", "00500", ExclusionReason.START_DATE_IN_FUTURE)
        snapshot = builder.build()

        anonymized = anonymize(snapshot)

        assert anonymized.records == {
            "00165": anonymize_record(ERIKA),
            "00200": anonymize_record(snapshot.records["00200"]),
        }
        assert anonymized.system_ids == snapshot.system_ids
        assert anonymized.excluded == snapshot.excluded
        assert anonymized.notices == snapshot.notices
        assert anonymized.email_owners == {"test-00165@example.com": ("1",), "test-00200@example.com": ("2",)}
        dumped = anonymized.model_dump_json()
        assert [value for value in REAL_VALUES if value in dumped] == []

    def test_original_is_unchanged(self) -> None:
        builder = SnapshotBuilder(System.PERSONIO)
        builder.add("1", ERIKA)
        snapshot = builder.build()
        anonymize(snapshot)
        assert snapshot.records == {"00165": ERIKA}
