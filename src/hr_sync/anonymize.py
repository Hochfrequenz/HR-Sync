"""
The anonymization in the test mode: test environments never get real names, e-mail addresses or phone numbers.

Strictly speaking, the data is pseudonymized, not anonymous: the employee number stays real, so whoever can see
Personio can tell whose data it is. The substitutes only depend on the employee number, so that a second run recognizes
an anonymized employee without deviations. They reveal nothing that the employee number doesn't reveal already, so no
secret key is needed.
"""

from collections.abc import Callable, Mapping

from hr_sync.model import EmployeeRecord, Snapshot, email_key

KEPT_FIELDS = frozenset(
    {
        "employee_number",
        "entry_date",
        "career_level",
        "office",
        "weekly_hours",
        "service_category",
        "vacation_days",
    }
)
"""
the fields that stay real: the employee number is the key, and the others are needed to test the mappings and the
public holidays in the test environment
"""

SUBSTITUTES: Mapping[str, Callable[[str], str]] = {
    "first_name": lambda employee_number: "Test",
    "last_name": lambda employee_number: f"Person-{employee_number}",
    "display_name": lambda employee_number: f"Test Person-{employee_number}",
    # example.com is reserved for examples, mails to it are never delivered
    "email": lambda employee_number: f"test-{employee_number}@example.com",
    # no German number continues with 0 after the country code, so this one belongs to nobody
    "mobile_phone": lambda employee_number: f"+49 000 {employee_number}",
}
"""the substitute of each anonymized field, by the employee number"""


def anonymize_record(record: EmployeeRecord) -> EmployeeRecord:
    """
    Returns the record with substitutes instead of the personal data.

    Only the fields in :data:`KEPT_FIELDS` and :data:`SUBSTITUTES` are part of the result. A field without a rule is
    dropped, so that a new field can't carry real values into a test environment unnoticed.
    """
    values: dict[str, object] = {field: getattr(record, field) for field in KEPT_FIELDS}
    values.update({field: substitute(record.employee_number) for field, substitute in SUBSTITUTES.items()})
    return EmployeeRecord.model_validate(values)


def anonymize(snapshot: Snapshot) -> Snapshot:
    """
    Returns the snapshot with anonymized records, see :func:`anonymize_record`.

    The e-mail addresses in :attr:`Snapshot.email_owners` are replaced by those of the anonymized records, so that no
    real address is left; the source's owners aren't used for planning anyway. Ids, exclusions and notices contain no
    personal data and stay as they are.
    """
    records = {number: anonymize_record(record) for number, record in snapshot.records.items()}
    email_owners = {
        email_key(record.email): (snapshot.system_ids[number],)
        for number, record in records.items()
        if record.email is not None and number in snapshot.system_ids
    }
    return snapshot.model_copy(update={"records": records, "email_owners": email_owners})
