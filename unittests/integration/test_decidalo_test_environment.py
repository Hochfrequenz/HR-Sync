"""
Integration tests against the Decidalo test environment, see section 8.3 of the implementation plan.

Personio is read in production, so a failing test must not print personal data:

- The assertions are not rewritten by pytest (PYTEST_DONT_REWRITE), because pytest would show the values of every
  subexpression, e.g. a snapshot with real names. Instead, every assertion has a message with employee numbers, field
  names, statuses and numbers only.
- :func:`_without_personal_data` reports any other error only with its :func:`hr_sync.events.error_fields`.
"""

import asyncio
import functools
from collections.abc import Callable
from datetime import date
from typing import ParamSpec

import pytest
from decidalo_client import DecidaloClient
from decidalo_client.models import ImportItemStatusType, UserBatchInput, UserImportResults, UserInput, UserOverview

from hr_sync.anonymize import KEPT_FIELDS, SUBSTITUTES, anonymize
from hr_sync.config import Settings
from hr_sync.events import error_fields
from hr_sync.model import EmployeeRecord, Snapshot, TargetSystem
from hr_sync.plan import Outcome
from hr_sync.sync import (
    Snapshots,
    Systems,
    connect,
    create_plan,
    execute_plan,
    load_snapshots,
    personio_attributes,
)
from hr_sync.systems.decidalo import load_decidalo
from hr_sync.systems.personio import load_personio

DECIDALO = [TargetSystem.DECIDALO]

P = ParamSpec("P")


def _without_personal_data(test: Callable[P, None]) -> Callable[P, None]:
    """
    Reports a failure of the test, including its cleanup, only with values that are safe to log: a failed assertion
    with its message, any other error with its type and :func:`hr_sync.events.error_fields`. The original exception,
    its message, its traceback and chained exceptions are not shown, because they may contain personal data.
    """

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        failure: str | None = None
        try:
            test(*args, **kwargs)
        except AssertionError as error:
            failure = f"AssertionError: {error}"
        except Exception as error:  # e.g. an API error or a validation error that repeats a name
            failure = f"{type(error).__name__}: {error_fields(error)}"
        if failure is not None:
            # outside the except blocks, so that the original exception isn't chained to the failure
            pytest.fail(failure, pytrace=False)

    return run


def _decidalo(systems: Systems) -> DecidaloClient:
    assert isinstance(systems.decidalo, DecidaloClient), "Decidalo has no client"
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


def _differing_fields(user: UserOverview, record: EmployeeRecord, settings: Settings) -> list[str]:
    """
    Returns the names of the fields in which the user in Decidalo differs from the anonymized record and the values
    for new users. The names of employee type and authorization role are not compared: they may differ in the test
    environment, the ids count.
    """
    expected = {
        "employeeID": record.employee_number,
        "displayName": record.display_name,
        "email": record.email,
        "entryDate": record.entry_date,
        "employeeTypeID": settings.decidalo_employee_type_id,
        "authorizationRoleID": settings.decidalo_authorization_role_id,
        "location": settings.decidalo_location,
        "countryCode": settings.decidalo_country_code,
        "companyName": settings.decidalo_company_name,
        "includeInResourceManagement": True,
        "hasLogin": False,
        "deactivationDate": None,
    }
    return [field for field, value in expected.items() if getattr(user, field) != value]


def _delete_test_user(client: DecidaloClient, employee_number: str, user_id: int) -> None:
    """
    Deletes the user that the test created and checks that it is gone.

    Only this user (by its id) is deleted, and only if its e-mail address is exactly the substitute, so that the test
    can never delete a real user or the user of another run.
    """
    substitute = SUBSTITUTES["email"](employee_number)
    users = [user for user in _users(client, employee_number) if user.userID == user_id]
    assert len(users) == 1, f"the test user {user_id} of employee {employee_number} is not there anymore"
    assert users[0].email == substitute, f"user {user_id} has no substitute address, so it is not deleted"
    response = _import(client, [UserInput(userID=user_id, employeeID=employee_number, email=substitute, delete=True)])
    statuses = [row.status for row in response.items or []]
    assert statuses == [ImportItemStatusType.Deleted], f"deleting user {user_id} returned {statuses}"
    left = [user.userID for user in _users(client, employee_number) if user.userID == user_id]
    assert left == [], f"user {user_id} is still there after deleting it"


@pytest.mark.integration
@_without_personal_data
def test_plan_with_anonymized_data(test_settings: Settings, events: list[dict[str, object]]) -> None:
    """
    Reads Personio and the Decidalo test environment, plans with anonymized data and executes the plan as a dry run.
    """
    systems = connect(test_settings, DECIDALO)
    real = load_personio(systems.personio, attributes=personio_attributes(test_settings), today=date.today())
    anonymized = anonymize(real)
    differing = sorted(real.records.keys() ^ anonymized.records.keys())
    assert differing == [], f"anonymizing changed the employees {differing}"
    # without the fields that stay real on purpose: a short name may occur in one of them, e.g. in an office
    leaked = _leaked(real, anonymized.model_dump_json(exclude={"records": {"__all__": set(KEPT_FIELDS)}}))
    assert leaked == [], f"real values left after anonymizing (employee number, field): {leaked}"

    snapshots = Snapshots(source=anonymized, targets={TargetSystem.DECIDALO: load_decidalo(_decidalo(systems))})
    sync_plan = create_plan(snapshots, max_employees=1)
    results = execute_plan(systems, sync_plan, test_settings, dry_run=True)

    planned = len(sync_plan.operations_for(TargetSystem.DECIDALO))
    assert planned <= 1, f"{planned} operations planned with max_employees=1"
    outcomes = sorted({result.outcome for result in results})
    assert set(outcomes) <= {Outcome.DRY_RUN}, f"outcomes of the dry run: {outcomes}"
    leaked = _leaked(real, str(events))
    assert leaked == [], f"real values in the events (employee number, field): {leaked}"


@pytest.mark.integration_write
@pytest.mark.usefixtures("allow_writes")
@_without_personal_data
def test_create_one_employee(test_settings: Settings, keep_test_data: bool, capsys: pytest.CaptureFixture[str]) -> None:
    """
    Creates the first employee of the plan anonymized in the Decidalo test environment, checks it and cleans up.

    Only a user that this run created is cleaned up: if Decidalo answers with another status than ``Created``, it
    matched an existing user, e.g. of another run, which the test must not delete.
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
    created_user_id: int | None = None
    try:
        results = execute_plan(systems, sync_plan, test_settings, dry_run=False)
        outcomes = [(result.outcome.value, result.detail) for result in results]
        assert outcomes == [("success", "Created")], f"employee {employee_number} was not created: {outcomes}"

        users = _users(client, employee_number)
        assert len(users) == 1, f"{len(users)} users with employee number {employee_number}, expected the new one"
        created_user_id = users[0].userID
        differing = _differing_fields(users[0], record, test_settings)
        assert differing == [], f"the new user of employee {employee_number} differs in {differing}"

        # a new plan has nothing left to do for the employee; Personio hasn't changed, so only Decidalo is loaded again
        snapshots = Snapshots(source=snapshots.source, targets={TargetSystem.DECIDALO: load_decidalo(client)})
        left = len(create_plan(snapshots, only={employee_number}).operations_for(TargetSystem.DECIDALO))
        assert left == 0, f"a new plan still has {left} operations for employee {employee_number}"
    finally:
        if created_user_id is not None and keep_test_data:
            with capsys.disabled():
                print(
                    f"\nkept the test user {created_user_id} with employee number {employee_number} in the Decidalo "
                    "test environment"
                )
        elif created_user_id is not None:
            _delete_test_user(client, employee_number, created_user_id)
