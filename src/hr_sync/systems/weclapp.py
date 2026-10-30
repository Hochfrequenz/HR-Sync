"""
The adapter of weclapp, a target system of HR-Sync.

Like every adapter, it separates IO from logic:

- loading: :func:`fetch_weclapp` only talks to weclapp, :func:`check_setup` checks that the tenant fits HR-Sync, and
  :func:`map_weclapp` builds the snapshot without IO. :func:`load_weclapp` does all of it and logs the event
  ``sync.load``.
- executing: :func:`send_employees` only talks to weclapp, :func:`evaluate_responses` turns the responses into results
  without IO, and :func:`execute_weclapp` does both and logs one event ``sync.operation`` per operation.

The weclapp client is synchronous. An employee consists of a user and an employee record (``employee``), which are
created with two requests, one after the other. The employee record holds the work schedule profile, whose start date
is the entry date.
"""

from collections.abc import Mapping, Sequence
from dataclasses import astuple, dataclass, field
from datetime import date
from decimal import Decimal
from typing import TypeVar

from weclapp_client import WeclappClient
from weclapp_client.exceptions import ConflictError, NotFoundError, WeclappError, WeclappValidationError
from weclapp_client.models import (
    CustomAttribute,
    CustomAttributeDefinition,
    CustomAttributeType,
    Employee,
    EmployeeCreate,
    IdReference,
    User,
    UserCreate,
    UserStatus,
    WorkScheduleProfileAssignment,
)

from hr_sync.config import ConfigurationError, ConfigurationProblem
from hr_sync.events import emit_operation, error_fields, wide_event
from hr_sync.mappings import (
    OFFICES,
    WECLAPP_ATTRIBUTE_IDS,
    WECLAPP_COST_RATE,
    WECLAPP_COST_RATE_REASON,
    WECLAPP_LICENSES,
    WECLAPP_USER_ROLE_IDS,
    WORK_SCHEDULE_PROFILES,
    WeclappAttributeIds,
)
from hr_sync.model import EmployeeRecord, Snapshot, SnapshotBuilder, System, TargetSystem
from hr_sync.plan import Operation, OperationResult, OperationType, Outcome

_RECORD_ERRORS = (WeclappValidationError, ConflictError)
"""errors that only concern the data of one employee; after any other error, the remaining operations are skipped"""

_NUMBER_TYPES = frozenset({CustomAttributeType.DECIMAL, CustomAttributeType.INTEGER})

# no PEP 695 syntax: HR-Sync supports Python 3.11
_T = TypeVar("_T")


@dataclass(frozen=True)
class WeclappSetup:
    """
    The ids in the weclapp tenant that HR-Sync reads and writes, and the values of new users.

    Besides the employee number and the status, the defaults are the fixed values of Hochfrequenz (see
    :mod:`hr_sync.mappings`). Tests replace them by the ids of a fake.
    """

    employee_number_attribute_id: str
    """the custom attribute definition of the employee number, see ``WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID``"""
    new_user_status: UserStatus = UserStatus.ACTIVE
    """the status of new users; a user with another status counts as deactivated"""
    attribute_ids: WeclappAttributeIds = WECLAPP_ATTRIBUTE_IDS
    state_value_ids: Mapping[str, str] = field(
        default_factory=lambda: {office.name: office.weclapp_state_value_id for office in OFFICES.values()}
    )
    """the selected value of the federal state by the name of the office"""
    work_schedule_profile_ids: Mapping[int, str] = field(default_factory=lambda: dict(WORK_SCHEDULE_PROFILES))
    """the work schedule profile by the weekly hours"""
    licenses: tuple[str, ...] = WECLAPP_LICENSES
    user_role_ids: tuple[str, ...] = WECLAPP_USER_ROLE_IDS
    cost_rate: Decimal = WECLAPP_COST_RATE
    cost_rate_reason: str = WECLAPP_COST_RATE_REASON

    @property
    def attribute_types(self) -> dict[str, frozenset[CustomAttributeType]]:
        """
        Returns the allowed types of the custom attribute definitions that HR-Sync reads or writes, by their ids.
        """
        ids = self.attribute_ids
        return {
            self.employee_number_attribute_id: frozenset({CustomAttributeType.STRING}),
            ids.cost_rate: _NUMBER_TYPES,
            ids.cost_rate_reason: frozenset({CustomAttributeType.STRING}),
            ids.vacation_days: _NUMBER_TYPES,
            ids.state: frozenset({CustomAttributeType.LIST}),
            ids.career_level: frozenset({CustomAttributeType.STRING}),
        }


@dataclass(frozen=True)
class LoadedWeclapp:
    """The data of weclapp, as loaded by :func:`fetch_weclapp`."""

    definitions: Mapping[str, CustomAttributeDefinition | None]
    """the custom attribute definitions of :attr:`WeclappSetup.attribute_types` by id; ``None`` if there is none"""
    work_schedule_profile_ids: frozenset[str]
    users: tuple[User, ...] = ()
    employees: tuple[Employee, ...] = ()


def load_weclapp(client: WeclappClient, *, setup: WeclappSetup, today: date) -> Snapshot:
    """
    Loads the users and employee records of weclapp, checks the tenant and maps the users onto the common model.

    See :func:`fetch_weclapp`, :func:`check_setup` and :func:`map_weclapp`. Logs one event ``sync.load``.

    Raises:
        ConfigurationError: if the tenant doesn't fit, see :func:`check_setup`.
    """
    with wide_event("sync.load", system=System.WECLAPP) as event:
        loaded = fetch_weclapp(client, setup)
        check_setup(loaded, setup)
        snapshot = map_weclapp(loaded, setup=setup, today=today)
        event["users"] = len(loaded.users)
        event["employees"] = len(loaded.employees)
        event["records"] = len(snapshot.records)
        event["incomplete"] = len(snapshot.incomplete)
        event["deactivated"] = len(snapshot.deactivated)
        event["notices"] = len(snapshot.notices)
        return snapshot


def fetch_weclapp(client: WeclappClient, setup: WeclappSetup) -> LoadedWeclapp:
    """
    Loads the custom attribute definitions and the work schedule profiles that HR-Sync uses, and all users and
    employee records, one request after the other.
    """
    definitions: dict[str, CustomAttributeDefinition | None] = {}
    for definition_id in setup.attribute_types:
        try:
            definitions[definition_id] = client.custom_attribute_definitions.get(definition_id)
        except NotFoundError:
            definitions[definition_id] = None
    return LoadedWeclapp(
        definitions=definitions,
        work_schedule_profile_ids=frozenset(profile.id for profile in client.work_schedule_profiles.iterate()),
        users=tuple(client.users.iterate()),
        employees=tuple(client.employees.iterate()),
    )


def check_setup(loaded: LoadedWeclapp, setup: WeclappSetup) -> None:
    """
    Checks that the tenant has the custom attributes and work schedule profiles that HR-Sync writes, without IO.

    Each custom attribute definition must exist, belong to users (``"user" in entities``; weclapp leaves
    ``attributeEntityType`` empty for them) and have the expected type: string, number (``DECIMAL`` or ``INTEGER``) or
    list. The type and the entity also catch an id that belongs to another attribute, e.g. in another tenant, unless
    that attribute has the same type. The employee number must not be one of the other attributes: its type would be
    checked as theirs, and HR-Sync would write two values into one attribute.

    Raises:
        ConfigurationError: with the ids that don't fit, and ``WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID`` if the employee
            number is one of them.
    """
    if setup.employee_number_attribute_id in astuple(setup.attribute_ids):
        raise ConfigurationError(
            ConfigurationProblem.WECLAPP_SETUP_MISMATCH,
            ["WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID"],
            [setup.employee_number_attribute_id],
        )
    mismatched = [
        definition_id
        for definition_id, types in setup.attribute_types.items()
        if not _fits(loaded.definitions.get(definition_id), types)
    ]
    missing_profiles = sorted(set(setup.work_schedule_profile_ids.values()) - loaded.work_schedule_profile_ids)
    if mismatched or missing_profiles:
        variables = ["WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID"] if setup.employee_number_attribute_id in mismatched else []
        raise ConfigurationError(
            ConfigurationProblem.WECLAPP_SETUP_MISMATCH, variables, [*mismatched, *missing_profiles]
        )


def _fits(definition: CustomAttributeDefinition | None, types: frozenset[CustomAttributeType]) -> bool:
    return definition is not None and definition.attribute_type in types and "user" in definition.entities


def map_weclapp(loaded: LoadedWeclapp, *, setup: WeclappSetup, today: date) -> Snapshot:
    """
    Maps the users of weclapp and their employee records onto the common model, without IO.

    The employee number is the custom attribute :attr:`WeclappSetup.employee_number_attribute_id`. A user without
    employee record is incomplete, a user with another status than :attr:`WeclappSetup.new_user_status` is
    deactivated (e.g. after leaving); the planner decides what that means. The fields of phase 2 are mapped back:

    - career level and vacation days from their custom attributes; vacation days that aren't whole days yield none
    - the office from the selected federal state, if exactly one office has it
    - the entry date from the earliest work schedule profile, the weekly hours from the one valid today (see
      :func:`current_assignment`); an unknown profile yields no weekly hours

    Values that can't be mapped back are missing, so that they show up as deviations.
    """
    employees_by_user: dict[str, Employee] = {}
    for loaded_employee in loaded.employees:
        if loaded_employee.user_id is not None:
            # weclapp allows only one employee record per user
            employees_by_user.setdefault(loaded_employee.user_id, loaded_employee)
    offices_by_state = {value_id: office for office, value_id in setup.state_value_ids.items()}
    hours_by_profile = {profile_id: hours for hours, profile_id in setup.work_schedule_profile_ids.items()}
    ids = setup.attribute_ids
    builder = SnapshotBuilder(System.WECLAPP)
    for user in loaded.users:
        employee_number = _string(user, setup.employee_number_attribute_id)
        if employee_number is None:
            builder.add_without_employee_number(user.id, user.email)
            continue
        employee = employees_by_user.get(user.id)
        assignments = employee.work_schedule_profile_assignments if employee is not None else []
        current = current_assignment(assignments, today)
        state = user.custom_attribute(ids.state)
        record = EmployeeRecord(
            employee_number=employee_number,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
            entry_date=min((assignment.start_date for assignment in assignments), default=None),
            career_level=_string(user, ids.career_level),
            office=offices_by_state.get(state.selected_value_id or "") if state is not None else None,
            weekly_hours=hours_by_profile.get(current.work_schedule_profile_id) if current is not None else None,
            vacation_days=_whole_number(user, ids.vacation_days),
        )
        builder.add(user.id, record, incomplete=employee is None, deactivated=user.status != setup.new_user_status)
    return builder.build()


def current_assignment(
    assignments: Sequence[WorkScheduleProfileAssignment], today: date
) -> WorkScheduleProfileAssignment | None:
    """
    Returns the work schedule profile that is valid today: it started today or earlier and has no end or ends today or
    later. If several are, the one with the latest start counts. Future and ended profiles don't count.
    """
    valid = [
        assignment
        for assignment in assignments
        if assignment.start_date <= today and (assignment.end_date is None or assignment.end_date >= today)
    ]
    return max(valid, key=lambda assignment: assignment.start_date, default=None)


def _string(user: User, definition_id: str) -> str | None:
    attribute = user.custom_attribute(definition_id)
    value = attribute.string_value if attribute is not None else None
    return (value.strip() or None) if value is not None else None


def _whole_number(user: User, definition_id: str) -> int | None:
    attribute = user.custom_attribute(definition_id)
    value = attribute.number_value if attribute is not None else None
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


@dataclass(frozen=True)
class NewEmployee:
    """What HR-Sync sends to weclapp for one operation."""

    user: UserCreate | None
    """the new user; ``None`` if the user exists already (:attr:`OperationType.COMPLETE`)"""
    user_id: str | None
    """the existing user; ``None`` if the user is created"""
    work_schedule_profile: WorkScheduleProfileAssignment
    """the only work schedule profile of the new employee record, from the entry date on"""


def new_employee(operation: Operation, setup: WeclappSetup) -> NewEmployee:
    """
    Returns the requests that create the employee of the operation in weclapp, as the admin's guide of Hochfrequenz
    says (see section 3.2 of the plan): an active user with licenses, roles and custom attributes, and an employee
    record with the work schedule profile of the weekly hours from the entry date on. The standard field ``title``
    stays empty, and the ``username`` is assigned by weclapp.

    The record comes from Personio, where all of these fields are mandatory and part of the mappings.

    Raises:
        ValueError: if the record lacks a field, which is a programming error.
    """
    record = operation.record
    weekly_hours = _required(record.weekly_hours, "weekly_hours")
    profile = WorkScheduleProfileAssignment(
        work_schedule_profile_id=setup.work_schedule_profile_ids[int(weekly_hours)],
        start_date=_required(record.entry_date, "entry_date"),
    )
    if operation.type == OperationType.COMPLETE:
        return NewEmployee(
            user=None, user_id=_required(operation.target_id, "target_id"), work_schedule_profile=profile
        )
    ids = setup.attribute_ids
    user = UserCreate(
        email=_required(record.email, "email"),
        status=setup.new_user_status,
        first_name=_required(record.first_name, "first_name"),
        last_name=_required(record.last_name, "last_name"),
        licenses=list(setup.licenses),
        user_roles=[IdReference(id=role_id) for role_id in setup.user_role_ids],
        custom_attributes=[
            CustomAttribute.of_string(setup.employee_number_attribute_id, record.employee_number),
            CustomAttribute.of_number(ids.cost_rate, setup.cost_rate),
            CustomAttribute.of_string(ids.cost_rate_reason, setup.cost_rate_reason),
            CustomAttribute.of_number(ids.vacation_days, _required(record.vacation_days, "vacation_days")),
            CustomAttribute.of_selection(ids.state, setup.state_value_ids[_required(record.office, "office")]),
            CustomAttribute.of_string(ids.career_level, _required(record.career_level, "career_level")),
        ],
    )
    return NewEmployee(user=user, user_id=None, work_schedule_profile=profile)


def _required(value: _T | None, field_name: str) -> _T:
    if value is None:
        raise ValueError(f"the record has no {field_name}")
    return value


@dataclass(frozen=True)
class SentEmployee:
    """What weclapp answered to the requests of one operation."""

    user_id: str | None = None
    """the created or existing user, if known"""
    employee_id: str | None = None
    """the created employee record"""
    error: WeclappError | None = None
    """the error of the request that failed: creating the user, or creating the employee record if ``user_id`` is set"""


def send_employees(client: WeclappClient, new_employees: Sequence[NewEmployee]) -> list[SentEmployee]:
    """
    Creates the employees one after the other: first the user (unless it exists), then the employee record.

    Stops after the first error that concerns weclapp as a whole, e.g. authentication, connection, rate limit or a
    server error. Validation errors and conflicts only concern one employee. Other exceptions than the errors of the
    weclapp client are programming errors and are not caught. The client doesn't retry a ``POST`` with unclear outcome;
    a user without employee record is completed by the next run.
    """
    sent: list[SentEmployee] = []
    for new in new_employees:
        result = _send(client, new)
        sent.append(result)
        if result.error is not None and not isinstance(result.error, _RECORD_ERRORS):
            break
    return sent


def _send(client: WeclappClient, new: NewEmployee) -> SentEmployee:
    user_id = new.user_id
    try:
        if new.user is not None:
            user_id = client.users.create(new.user).id
        employee = client.employees.create(
            EmployeeCreate(
                user_id=_required(user_id, "user_id"), work_schedule_profile_assignments=[new.work_schedule_profile]
            )
        )
    except WeclappError as error:
        return SentEmployee(user_id=user_id, error=error)
    return SentEmployee(user_id=user_id, employee_id=employee.id)


def evaluate_responses(operations: Sequence[Operation], sent: Sequence[SentEmployee]) -> list[OperationResult]:
    """
    Returns the result of each operation, from the answer at the same position.

    An operation succeeds if user and employee record exist afterwards. If the user was created, but not its employee
    record, the operation is an error with ``detail=employee_not_created``; the next run completes it. Operations
    without answer were skipped after an error that concerns weclapp as a whole.
    """
    results: list[OperationResult] = []
    for index, operation in enumerate(operations):
        if index >= len(sent):
            results.append(OperationResult(operation=operation, outcome=Outcome.SKIPPED))
            continue
        answer = sent[index]
        if answer.error is None:
            results.append(OperationResult(operation=operation, outcome=Outcome.SUCCESS, target_id=answer.user_id))
            continue
        created_user = operation.type == OperationType.CREATE and answer.user_id is not None
        results.append(
            OperationResult(
                operation=operation,
                outcome=Outcome.ERROR,
                target_id=answer.user_id,
                detail="employee_not_created" if created_user else None,
                error=error_fields(answer.error),
            )
        )
    return results


def execute_weclapp(
    client: WeclappClient, operations: Sequence[Operation], *, setup: WeclappSetup
) -> list[OperationResult]:
    """
    Creates the employees of the operations in weclapp, one after the other (see :func:`send_employees`), and logs one
    event ``sync.operation`` per operation, without the messages of weclapp, which may contain personal data.

    Raises:
        ValueError: if an operation is not a weclapp ``CREATE`` or ``COMPLETE``, or a ``COMPLETE`` lacks the user.
    """
    for operation in operations:
        if operation.system != TargetSystem.WECLAPP:
            raise ValueError(f"weclapp can only execute operations for weclapp, got one for {operation.system}")
    if not operations:
        return []
    new_employees = [new_employee(operation, setup) for operation in operations]
    results = evaluate_responses(operations, send_employees(client, new_employees))
    for result in results:
        emit_operation(result)
    return results
