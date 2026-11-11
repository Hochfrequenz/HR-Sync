"""
The report of a run for the HR management, sent by mail through Mailgun (section 7.2 of the plan).

Like the adapters, it separates IO from logic:

- :func:`build_report` collects what there is to report: created employees, problems while creating, data problems
  in the systems, deviations and an abort.
- :func:`render_report` turns it into the subject and the text of the mail.
- :func:`send_report` sends the mail through the HTTP API of Mailgun.

The mail is written in German for the HR management, short and not technical. Like the events, it contains no
personal data besides employee numbers and the ids of records without one, because Mailgun is an external service.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import httpx
from decidalo_client.models import ImportItemStatusType

from hr_sync.config import ConfigurationError, ConfigurationProblem, Settings
from hr_sync.events import error_fields
from hr_sync.model import ExclusionReason, Notice, NoticeReason, System, TargetSystem
from hr_sync.plan import OperationResult, OperationType, Outcome, SyncPlan

_SYSTEM_NAMES: Mapping[str, str] = {"personio": "Personio", "weclapp": "weclapp", "decidalo": "Decidalo"}
"""the names of the systems, by the value of :class:`System` or :class:`TargetSystem`"""

_ID_NAMES: Mapping[str, str] = {"personio": "Personio-ID", "weclapp": "User-ID", "decidalo": "User-ID"}
"""what the id of a record is called in its system, for records without employee number"""

FIELD_LABELS: Mapping[str, str] = {
    "first_name": "Vorname",
    "last_name": "Nachname",
    "display_name": "Anzeigename",
    "email": "E-Mail-Adresse",
    "entry_date": "Eintrittsdatum",
    "career_level": "Karrierestufe",
    "mobile_phone": "Mobilnummer",
    "office": "Standort",
    "weekly_hours": "Wochenstunden",
    "service_category": "Service-Kategorie",
    "vacation_days": "Urlaubsanspruch",
}
"""the fields of :class:`hr_sync.model.EmployeeRecord` in words"""

NOTICE_TEXTS: Mapping[NoticeReason, str] = {
    NoticeReason.MISSING_EMPLOYEE_NUMBER: "keine MA-Nummer hinterlegt",
    NoticeReason.DUPLICATE_EMPLOYEE_NUMBER: "MA-Nummer mehrfach vergeben",
    NoticeReason.MISSING_EMAIL: "keine E-Mail-Adresse hinterlegt",
    NoticeReason.MISSING_ENTRY_DATE: "kein Eintrittsdatum hinterlegt",
    NoticeReason.NOT_RELEVANT: "angefragt, aber in Personio nicht aktiv.",
    NoticeReason.UNKNOWN_EMPLOYEE_NUMBER: "angefragt, aber in Personio nicht gefunden.",
    NoticeReason.EMAIL_IN_USE: (
        "nicht angelegt, weil die E-Mail-Adresse dort schon einem anderen User gehört. Bitte prüfen."
    ),
    NoticeReason.DUPLICATE_EMAIL: "E-Mail-Adresse mehrfach vergeben",
    NoticeReason.DEACTIVATED_IN_TARGET: "nicht angelegt, weil der Mitarbeiter dort deaktiviert ist. Bitte prüfen.",
    NoticeReason.MISSING_FIELD: "{field} fehlt",
    NoticeReason.UNMAPPED_VALUE: "{field} unbekannt",
    NoticeReason.INVALID_PHONE_NUMBER: "{field} ungültig",
    NoticeReason.VALUE_TOO_LONG: "{field} zu lang",
}
"""the notices in words; ``{field}`` is the field of the detail, see :data:`FIELD_LABELS`"""

_FUTURE_START_TEXT = "angefragt, aber das Eintrittsdatum liegt noch in der Zukunft."
"""the text of a notice ``not_relevant`` whose employment is active, but starts later"""

_EXISTING_USER_TEXTS: Mapping[str, str] = {
    ImportItemStatusType.Updated: (
        "nicht angelegt, weil Decidalo einen bestehenden User gefunden und geändert hat. Bitte prüfen."
    ),
    ImportItemStatusType.NotModified: (
        "nicht angelegt, weil Decidalo einen bestehenden User mit denselben Daten gefunden hat. Bitte prüfen."
    ),
}
"""
the results of Decidalo for a new user that hit an existing one, by row status (see
:func:`hr_sync.systems.decidalo.evaluate_responses`); unlike other errors, the next run can't be relied on to fix them
"""

_CONFIGURATION_TEXTS: Mapping[ConfigurationProblem, str] = {
    ConfigurationProblem.MISSING_VARIABLES: "In der Konfiguration fehlen Einstellungen.",
    ConfigurationProblem.INVALID_VARIABLES: "Die Konfiguration enthält ungültige Einstellungen.",
    ConfigurationProblem.WECLAPP_SETUP_MISMATCH: "weclapp passt nicht zur Einrichtung von HR-Sync.",
}

_GREEN_LIGHT = "\N{LARGE GREEN CIRCLE}"
"""ends the line of every created employee"""

_RED_LIGHT = "\N{LARGE RED CIRCLE}"
"""ends the line of every problem while creating and the reason of an abort"""

_YELLOW_LIGHT = "\N{LARGE YELLOW CIRCLE}"
"""ends the line of every data problem and every line of the deviations"""

_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Report:
    """What there is to report about a run, already in words, without personal data."""

    created: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """the systems in which each employee was created (or would be, in a dry run), by employee number"""
    problems: tuple[str, ...] = ()
    """the problems while creating, one line per employee and system"""
    data_errors: Mapping[System, tuple[str, ...]] = field(default_factory=dict)
    """the data problems in each system, one line per problem"""
    deviations: Mapping[TargetSystem, tuple[str, ...]] = field(default_factory=dict)
    """the employee numbers of existing employees with deviations, per target system"""
    aborted: str | None = None
    """why the run was aborted, if it was"""

    @property
    def is_empty(self) -> bool:
        """True if there is nothing to report, so that no mail is sent."""
        return not (self.created or self.problems or self.data_errors or self.deviations or self.aborted)

    @property
    def counts(self) -> dict[str, int]:
        """the number of entries per section, for the event ``sync.report``"""
        return {
            "created": len(self.created),
            "problems": len(self.problems),
            "data_errors": sum(len(lines) for lines in self.data_errors.values()),
            "deviations": sum(len(numbers) for numbers in self.deviations.values()),
        }


@dataclass(frozen=True)
class Mail:
    """The mail with the report."""

    subject: str
    text: str


def build_report(
    *,
    snapshot_notices: Sequence[Notice] = (),
    sync_plan: SyncPlan | None = None,
    results: Sequence[OperationResult] = (),
    aborted: str | None = None,
) -> Report:
    """
    Collects what there is to report about a run, without IO.

    The notices of the snapshots are data problems in the systems. The notices of the plan and the failed or skipped
    operations are problems while creating. Successful operations, and in a dry run the planned ones, count as created.
    """
    created: dict[str, list[str]] = defaultdict(list)
    problems: list[tuple[str, int, str]] = []
    for result in results:
        operation = result.operation
        number = operation.record.employee_number
        system = _SYSTEM_NAMES[operation.system.value]
        if result.outcome in (Outcome.SUCCESS, Outcome.DRY_RUN):
            completed = operation.type == OperationType.COMPLETE
            created[number].append(f"{system} (Personalakte ergänzt)" if completed else system)
        else:
            problems.append((number, _order(operation.system.value), f"{number} ({system}): {_result_text(result)}"))
    notices = sync_plan.notices if sync_plan is not None else ()
    problems.extend(
        (notice.employee_number or "", _order(notice.system.value), _problem_line(notice)) for notice in notices
    )
    data_errors: dict[System, list[str]] = defaultdict(list)
    for notice in snapshot_notices:
        data_errors[notice.system].append(f"{_subject(notice)}: {_notice_text(notice)}")
    deviations: dict[TargetSystem, list[str]] = defaultdict(list)
    for employee in sync_plan.employees if sync_plan is not None else ():
        for deviation in employee.deviations:
            deviations[deviation.system].append(employee.employee_number)
    return Report(
        created={number: tuple(created[number]) for number in sorted(created)},
        problems=tuple(line for *_, line in sorted(problems)),
        data_errors={system: tuple(sorted(data_errors[system])) for system in System if system in data_errors},
        deviations={target: tuple(sorted(deviations[target])) for target in TargetSystem if target in deviations},
        aborted=aborted,
    )


def abort_reason(error: Exception, *, executing: bool) -> str:
    """
    Returns why the run was aborted, in words: the problem of the configuration, or the system whose adapter raised the
    error (see :func:`hr_sync.events.error_fields`), and whether anything may have been created.
    """
    if isinstance(error, ConfigurationError):
        reason = _CONFIGURATION_TEXTS.get(error.problem, "Die Konfiguration passt nicht.")
    else:
        module = error_fields(error).get("module")
        system = module.removeprefix("hr_sync.systems.") if isinstance(module, str) else ""
        if system in _SYSTEM_NAMES:
            reason = f"{_SYSTEM_NAMES[system]} war nicht erreichbar oder hat einen Fehler gemeldet."
        else:
            reason = "Ein unerwarteter Fehler ist aufgetreten."
    if executing:
        return f"{reason} Ein Teil kann schon angelegt sein, Details stehen im Protokoll."
    return f"{reason} Es wurde nichts angelegt."


def render_report(report: Report, *, targets: Sequence[TargetSystem], today: date, run_id: str, dry_run: bool) -> Mail:
    """
    Returns the mail with the report, without IO. Only the sections with content are part of it. A dry run is marked
    as such, in the subject and in the text.

    Every entry ends with a traffic light for its kind, in a real run and in a dry run, never in the subject: green
    for a created employee, red for a problem while creating or an abort, yellow for a data problem or deviations.
    """
    if dry_run:
        blocks = ["Probelauf: HR-Sync hat nichts geschrieben. So sähe der Bericht aus."]
    else:
        blocks = [f"HR-Sync hat Personio mit {_enumeration(targets)} abgeglichen."]
    if report.aborted:
        blocks.append(f"ABGEBROCHEN\n{report.aborted} {_RED_LIGHT}")
    if report.created:
        heading = "WÜRDE ANGELEGT" if dry_run else "ANGELEGT"
        lines = [_item(f"{number}: {', '.join(systems)}", _GREEN_LIGHT) for number, systems in report.created.items()]
        blocks.append("\n".join([heading, *lines]))
    if report.problems:
        blocks.append("\n".join(["PROBLEME BEIM ANLEGEN", *(_item(line, _RED_LIGHT) for line in report.problems)]))
    if report.data_errors:
        lines = [
            "DATENFEHLER",
            "Mitarbeiter mit Datenfehlern in Personio legt HR-Sync erst an, wenn der Fehler behoben ist.",
        ]
        for system, errors in report.data_errors.items():
            lines.append(_SYSTEM_NAMES[system.value])
            lines.extend(_item(line, _YELLOW_LIGHT) for line in errors)
        blocks.append("\n".join(lines))
    if report.deviations:
        lines = ["ABWEICHUNGEN", "Bestehende Mitarbeiter mit anderen Daten als in Personio. HR-Sync ändert sie nicht."]
        lines.extend(
            _item(f"{_SYSTEM_NAMES[target.value]}: {len(numbers)} ({', '.join(numbers)})", _YELLOW_LIGHT)
            for target, numbers in report.deviations.items()
        )
        blocks.append("\n".join(lines))
    blocks.append(f"Lauf vom {today:%d.%m.%Y}, Lauf-ID {run_id}")
    subject = f"HR-Sync {today:%d.%m.%Y}: {_summary(report)}"
    return Mail(subject=f"Probelauf: {subject}" if dry_run else subject, text="\n\n".join(blocks) + "\n")


def send_report(mail: Mail, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
    """
    Sends the mail to the recipients of the report, through the HTTP API of Mailgun. It's not repeated after an
    error: a mail that arrives twice is worse than an error in the event ``sync.report``.

    Raises:
        httpx.HTTPError: if Mailgun can't be reached or rejects the mail.
        ValueError: if Mailgun is not configured, which :func:`hr_sync.config.check_settings` reports beforehand.
    """
    api_key, domain, sender = settings.mailgun_api_key, settings.mailgun_domain, settings.hr_sync_report_from
    if api_key is None or domain is None or sender is None:
        raise ValueError("Mailgun is not configured")
    url = f"{settings.mailgun_base_url.rstrip('/')}/v3/{domain}/messages"
    data = {"from": sender, "to": ", ".join(settings.hr_sync_report_to), "subject": mail.subject, "text": mail.text}
    with httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS) as client:
        response = client.post(url, auth=("api", api_key.get_secret_value()), data=data)
    response.raise_for_status()


def _item(line: str, light: str) -> str:
    return f"- {line} {light}"


def _order(system: str) -> int:
    return list(_SYSTEM_NAMES).index(system)


def _result_text(result: OperationResult) -> str:
    system = _SYSTEM_NAMES[result.operation.system.value]
    action = "Personalakte nicht ergänzt" if result.operation.type == OperationType.COMPLETE else "nicht angelegt"
    if result.outcome == Outcome.SKIPPED:
        return f"{action}, weil {system} vorher einen Fehler gemeldet hat. Der nächste Lauf versucht es erneut."
    if result.detail == "invitation_not_sent":
        return f"angelegt, aber die Einladung ging nicht raus. Bitte in {system} per Hand einladen."
    if result.detail == "employee_not_created":
        return "User angelegt, aber ohne Personalakte. Der nächste Lauf ergänzt sie, einladen bitte per Hand."
    existing_user_text = _EXISTING_USER_TEXTS.get(result.detail or "")
    if result.operation.system == TargetSystem.DECIDALO and existing_user_text is not None:
        return existing_user_text
    return f"{action}, {system} hat einen Fehler gemeldet. Der nächste Lauf versucht es erneut."


def _problem_line(notice: Notice) -> str:
    number = notice.employee_number or _subject(notice)
    if notice.system == System.PERSONIO:
        return f"{number}: {_notice_text(notice)}"
    return f"{number} ({_SYSTEM_NAMES[notice.system.value]}): {_notice_text(notice)}"


def _subject(notice: Notice) -> str:
    if notice.employee_number is not None:
        return notice.employee_number
    return f"{_ID_NAMES[notice.system.value]} {notice.system_id}"


def _notice_text(notice: Notice) -> str:
    if notice.reason == NoticeReason.NOT_RELEVANT and notice.detail == ExclusionReason.START_DATE_IN_FUTURE:
        return _FUTURE_START_TEXT
    label = FIELD_LABELS.get(notice.detail or "", notice.detail or "Ein Feld")
    return NOTICE_TEXTS[notice.reason].format(field=label)


def _enumeration(targets: Sequence[TargetSystem]) -> str:
    names = [_SYSTEM_NAMES[target.value] for target in targets]
    if len(names) < 2:
        return "".join(names) or "keinem Zielsystem"
    return f"{', '.join(names[:-1])} und {names[-1]}"


def _summary(report: Report) -> str:
    counts = report.counts
    parts = []
    if report.aborted:
        parts.append("Lauf abgebrochen")
    if counts["created"]:
        parts.append(f"{counts['created']} angelegt")
    if counts["problems"]:
        parts.append(_count(counts["problems"], "Problem", "Probleme"))
    if counts["data_errors"]:
        parts.append(f"{counts['data_errors']} Datenfehler")
    if not parts and counts["deviations"]:
        parts.append(_count(counts["deviations"], "Abweichung", "Abweichungen"))
    return ", ".join(parts)


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"
