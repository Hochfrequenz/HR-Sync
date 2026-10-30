"""
Integration test against weclapp, see section 8.3 of the implementation plan.

There is no weclapp test tenant, so the test runs against production, but never stores anything: it only reads, and
it sends the requests of the first employee of the plan as a dry run (``dryRun=true``), which weclapp validates without
storing. The dry run sends the real data of that employee to weclapp, the system that gets them anyway.

Personio and weclapp are read in production, so a failing test must not print personal data:

- The assertions are not rewritten by pytest (PYTEST_DONT_REWRITE). Instead, every assertion has a message with
  employee numbers, field names, statuses and numbers only.
- :func:`unittests.integration.guards.without_personal_data` reports any other error only with its
  :func:`hr_sync.events.error_fields`.
"""

from datetime import date

import pytest
from weclapp_client import WeclappClient
from weclapp_client.models import EmployeeCreate

from hr_sync.config import Settings
from hr_sync.model import TargetSystem
from hr_sync.plan import Outcome
from hr_sync.sync import Systems, connect, create_plan, execute_plan, load_snapshots, weclapp_setup
from hr_sync.systems.weclapp import new_employee
from unittests.integration.guards import leaked, without_personal_data

WECLAPP = [TargetSystem.WECLAPP]


def _weclapp(systems: Systems) -> WeclappClient:
    assert isinstance(systems.weclapp, WeclappClient), "weclapp has no client"
    return systems.weclapp


@pytest.mark.integration
@without_personal_data
def test_plan_and_dry_run(weclapp_settings: Settings, events: list[dict[str, object]]) -> None:
    """
    Loads Personio and weclapp, which also checks the custom attributes and work schedule profiles of the tenant, plans
    with weclapp as target and sends the requests of the first employee of the plan as a dry run.
    """
    systems = connect(weclapp_settings, WECLAPP)
    client = _weclapp(systems)
    try:
        users_before = client.users.count()
        snapshots = load_snapshots(systems, weclapp_settings, WECLAPP, date.today())
        sync_plan = create_plan(snapshots, max_employees=1)
        results = execute_plan(systems, sync_plan, weclapp_settings, dry_run=True)
        outcomes = sorted({result.outcome for result in results})
        assert set(outcomes) <= {Outcome.DRY_RUN}, f"outcomes of the dry run: {outcomes}"

        operations = sync_plan.operations_for(TargetSystem.WECLAPP)
        assert len(operations) <= 1, f"{len(operations)} operations planned with max_employees=1"
        for operation in operations:
            new = new_employee(operation, weclapp_setup(weclapp_settings))
            if new.user is not None:
                client.users.create(new.user, dry_run=True)
            else:
                employee = EmployeeCreate(
                    user_id=str(new.user_id), work_schedule_profile_assignments=[new.work_schedule_profile]
                )
                client.employees.create(employee, dry_run=True)

        users_after = client.users.count()
        assert users_after == users_before, f"weclapp has {users_after} users instead of {users_before}"
        found = leaked(snapshots.source, str(events))
        assert found == [], f"real values in the events (employee number, field): {found}"
    finally:
        systems.close()
