"""
Integration tests against the Decidalo test environment, see section 8.3 of the implementation plan.

Personio is read in production. That's why the assertions only compare employee numbers, field names, statuses and
substitutes: a failing test must not print personal data.
"""

import asyncio
from datetime import date

import pytest
from decidalo_client import DecidaloClient
from decidalo_client.models import ImportItemStatusType, UserBatchInput, UserImportResults, UserInput, UserOverview

from hr_sync.anonymize import SUBSTITUTES, anonymize
from hr_sync.config import Settings
from hr_sync.model import EmployeeRecord, Snapshot, TargetSystem
from hr_sync.plan import Outcome
from hr_sync.sync import Snapshots, Systems, connect, create_plan, execute_plan, load_snapshots
from hr_sync.systems.decidalo import load_decidalo
from hr_sync.systems.personio import load_personio

DECIDALO = [TargetSystem.DECIDALO]


def _decidalo(systems: Systems) -> DecidaloClient:
    assert isinstance(systems.decidalo, DecidaloClient)
    return systems.decidalo


def _leaked(real: Snapshot, text: str) -> list[tuple[str, str]]:
    """
    Returns employee number and field of every real value that occurs in the text, without the value itself.
    """
    return [
        (number, field)
        for number, record in real.records.items()
        for field in SUBSTITUTES
        if isinstance(value := getattr(record, field), str) and value in text
    ]


def _users(client: DecidaloClient, employee_number: str) -> list[UserOverview]:
    async def get() -> list[UserOverview]:
        async with client:
            return await client.get_users(employee_id=employee_number)

    return asyncio.run(get())


def _import(client: DecidaloClient, users: list[UserInput]) -> UserImportResults:
    async def send() -> UserImportResults:
        async with client:
            return await client.import_users_sync(UserBatchInput(users=users))

    return asyncio.run(send())


def _differing_fields(user: UserOverview, record: EmployeeRecord) -> list[str]:
    """
    Returns the names of the fields in which the user in Decidalo differs from the anonymized record.
    """
    expected = {
        "employeeID": record.employee_number,
        "displayName": record.display_name,
        "email": record.email,
        "entryDate": record.entry_date,
        "hasLogin": False,
        "deactivationDate": None,
    }
    return [field for field, value in expected.items() if getattr(user, field) != value]


def _delete_test_user(client: DecidaloClient, employee_number: str) -> None:
    """
    Deletes the anonymized test user with the employee number and checks that it is gone.

    Only a user whose e-mail address is exactly the substitute is deleted, so that the test can never delete a real
    user. If another user has the employee number, nothing is deleted and the test fails.
    """
    substitute = SUBSTITUTES["email"](employee_number)
    users = _users(client, employee_number)
    others = [user for user in users if user.email != substitute]
    assert len(others) == 0, f"another user has the employee number {employee_number}, nothing is deleted"
    if not users:
        return
    rows = [UserInput(userID=user.userID, employeeID=employee_number, email=substitute, delete=True) for user in users]
    response = _import(client, rows)
    assert [row.status for row in response.items or []] == [ImportItemStatusType.Deleted] * len(rows)
    assert len(_users(client, employee_number)) == 0


@pytest.mark.integration
def test_plan_with_anonymized_data(test_settings: Settings, events: list[dict[str, object]]) -> None:
    """
    Reads Personio and the Decidalo test environment, plans with anonymized data and executes the plan as a dry run.
    """
    systems = connect(test_settings, DECIDALO)
    real = load_personio(
        systems.personio, employee_number_attribute=test_settings.personio_employee_number_attribute, today=date.today()
    )
    anonymized = anonymize(real)
    assert anonymized.records.keys() == real.records.keys()
    assert _leaked(real, anonymized.model_dump_json()) == []

    snapshots = Snapshots(source=anonymized, targets={TargetSystem.DECIDALO: load_decidalo(_decidalo(systems))})
    sync_plan = create_plan(snapshots, max_employees=1)
    results = execute_plan(systems, sync_plan, test_settings, dry_run=True)

    assert len(sync_plan.operations_for(TargetSystem.DECIDALO)) <= 1
    assert {result.outcome for result in results} <= {Outcome.DRY_RUN}
    assert _leaked(real, str(events)) == []


@pytest.mark.integration_write
@pytest.mark.usefixtures("allow_writes")
def test_create_one_employee(test_settings: Settings, keep_test_data: bool, capsys: pytest.CaptureFixture[str]) -> None:
    """
    Creates the first employee of the plan anonymized in the Decidalo test environment, checks it and cleans up.
    """
    systems = connect(test_settings, DECIDALO)
    client = _decidalo(systems)
    snapshots = load_snapshots(systems, test_settings, DECIDALO, date.today())
    sync_plan = create_plan(snapshots, max_employees=1)
    operations = sync_plan.operations_for(TargetSystem.DECIDALO)
    if not operations:
        pytest.skip("no employee is missing in the Decidalo test environment")
    (operation,) = operations
    record = operation.record
    employee_number = record.employee_number
    try:
        results = execute_plan(systems, sync_plan, test_settings, dry_run=False)
        assert [(result.outcome, result.detail) for result in results] == [(Outcome.SUCCESS, "Created")]

        users = _users(client, employee_number)
        assert len(users) == 1
        assert _differing_fields(users[0], record) == []

        # a new plan has nothing left to do for the employee; Personio hasn't changed, so only Decidalo is loaded again
        snapshots = Snapshots(source=snapshots.source, targets={TargetSystem.DECIDALO: load_decidalo(client)})
        assert create_plan(snapshots, only={employee_number}).operations_for(TargetSystem.DECIDALO) == ()
    finally:
        if keep_test_data:
            with capsys.disabled():
                print(f"\nkept the test user with employee number {employee_number} in the Decidalo test environment")
        else:
            _delete_test_user(client, employee_number)
