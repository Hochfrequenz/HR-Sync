import base64
from datetime import date

import httpx
import pytest
from decidalo_client.models import ImportItemStatusType
from pydantic import SecretStr

from hr_sync.config import ConfigurationError, ConfigurationProblem, Settings
from hr_sync.model import EmployeeRecord, ExclusionReason, Notice, NoticeReason, System, TargetSystem
from hr_sync.plan import Deviation, EmployeePlan, Operation, OperationResult, OperationType, Outcome, SyncPlan
from hr_sync.report import (
    FIELD_LABELS,
    NOTICE_TEXTS,
    Mail,
    Report,
    abort_reason,
    build_report,
    render_report,
    send_report,
)
from hr_sync.systems.weclapp import weclapp_phone_number

TODAY = date(2026, 11, 6)
RUN_ID = "3f2a9c"
BOTH = (TargetSystem.WECLAPP, TargetSystem.DECIDALO)


def _record(employee_number: str) -> EmployeeRecord:
    return EmployeeRecord(
        employee_number=employee_number, first_name="Erika", last_name="Musterfrau", email="erika@example.com"
    )


def _result(
    employee_number: str,
    system: TargetSystem,
    outcome: Outcome,
    *,
    type_: OperationType = OperationType.CREATE,
    detail: str | None = None,
) -> OperationResult:
    operation = Operation(system=system, type=type_, record=_record(employee_number))
    return OperationResult(operation=operation, outcome=outcome, detail=detail)


def _notice(
    system: System,
    reason: NoticeReason,
    *,
    employee_number: str | None = None,
    system_id: str | None = None,
    detail: str | None = None,
) -> Notice:
    return Notice(system=system, reason=reason, employee_number=employee_number, system_id=system_id, detail=detail)


def _plan(*employees: EmployeePlan, notices: tuple[Notice, ...] = ()) -> SyncPlan:
    return SyncPlan(employees=employees, only_in_target={}, notices=notices)


class TestBuildReport:
    """
    Tests of collecting what there is to report, without IO.
    """

    def test_created_per_employee(self) -> None:
        report = build_report(
            results=[
                _result("00974", TargetSystem.WECLAPP, Outcome.SUCCESS, type_=OperationType.COMPLETE),
                _result("00965", TargetSystem.WECLAPP, Outcome.SUCCESS),
                _result("00965", TargetSystem.DECIDALO, Outcome.SUCCESS),
            ]
        )
        assert report.created == {
            "00965": ("weclapp", "Decidalo"),
            "00974": ("weclapp (Personalakte ergänzt)",),
        }
        assert report.problems == ()

    def test_dry_run_counts_as_created(self) -> None:
        report = build_report(results=[_result("00965", TargetSystem.WECLAPP, Outcome.DRY_RUN)])
        assert report.created == {"00965": ("weclapp",)}

    def test_problems_while_creating(self) -> None:
        report = build_report(
            results=[
                _result("00981", TargetSystem.WECLAPP, Outcome.ERROR, detail="invitation_not_sent"),
                _result("00974", TargetSystem.WECLAPP, Outcome.ERROR, detail="employee_not_created"),
                _result("00990", TargetSystem.WECLAPP, Outcome.ERROR, type_=OperationType.COMPLETE),
                _result("00965", TargetSystem.DECIDALO, Outcome.ERROR, detail="Failed"),
                _result("00970", TargetSystem.DECIDALO, Outcome.SKIPPED),
            ]
        )
        assert report.created == {}
        assert report.problems == (
            "00965 (Decidalo): nicht angelegt, Decidalo hat einen Fehler gemeldet. Der nächste Lauf versucht es "
            "erneut.",
            "00970 (Decidalo): nicht angelegt, weil Decidalo vorher einen Fehler gemeldet hat. Der nächste Lauf "
            "versucht es erneut.",
            "00974 (weclapp): User angelegt, aber ohne Personalakte. Der nächste Lauf ergänzt sie, einladen bitte "
            "per Hand.",
            "00981 (weclapp): angelegt, aber die Einladung ging nicht raus. Bitte in weclapp per Hand einladen.",
            "00990 (weclapp): Personalakte nicht ergänzt, weclapp hat einen Fehler gemeldet. Der nächste Lauf "
            "versucht es erneut.",
        )

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param(
                ImportItemStatusType.Updated,
                "nicht angelegt, weil Decidalo einen bestehenden User gefunden und geändert hat. Bitte prüfen.",
                id="updated",
            ),
            pytest.param(
                ImportItemStatusType.NotModified,
                "nicht angelegt, weil Decidalo einen bestehenden User mit denselben Daten gefunden hat. Bitte prüfen.",
                id="not_modified",
            ),
        ],
    )
    def test_existing_user_in_decidalo(self, status: ImportItemStatusType, expected: str) -> None:
        report = build_report(results=[_result("00965", TargetSystem.DECIDALO, Outcome.ERROR, detail=status.value)])
        assert report.problems == (f"00965 (Decidalo): {expected}",)

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            pytest.param(
                ExclusionReason.EMPLOYMENT_NOT_ACTIVE, "angefragt, aber in Personio nicht aktiv.", id="inactive"
            ),
            pytest.param(
                ExclusionReason.START_DATE_IN_FUTURE,
                "angefragt, aber das Eintrittsdatum liegt noch in der Zukunft.",
                id="future_start",
            ),
        ],
    )
    def test_not_relevant(self, reason: ExclusionReason, expected: str) -> None:
        notice = _notice(System.PERSONIO, NoticeReason.NOT_RELEVANT, employee_number="00999", detail=reason.value)
        report = build_report(sync_plan=_plan(notices=(notice,)))
        assert report.problems == (f"00999: {expected}",)

    def test_problems_found_while_planning(self) -> None:
        report = build_report(
            sync_plan=_plan(
                notices=(
                    _notice(System.WECLAPP, NoticeReason.EMAIL_IN_USE, employee_number="00974"),
                    _notice(System.DECIDALO, NoticeReason.DEACTIVATED_IN_TARGET, employee_number="00965"),
                    _notice(System.PERSONIO, NoticeReason.UNKNOWN_EMPLOYEE_NUMBER, employee_number="00999"),
                )
            )
        )
        assert report.problems == (
            "00965 (Decidalo): nicht angelegt, weil der Mitarbeiter dort deaktiviert ist. Bitte prüfen.",
            "00974 (weclapp): nicht angelegt, weil die E-Mail-Adresse dort schon einem anderen User gehört. "
            "Bitte prüfen.",
            "00999: angefragt, aber in Personio nicht gefunden.",
        )

    def test_data_errors_per_system(self) -> None:
        report = build_report(
            snapshot_notices=[
                _notice(System.WECLAPP, NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id="1234"),
                _notice(System.PERSONIO, NoticeReason.UNMAPPED_VALUE, employee_number="00123", detail="career_level"),
                _notice(System.PERSONIO, NoticeReason.MISSING_EMPLOYEE_NUMBER, system_id="4711"),
                _notice(
                    System.PERSONIO, NoticeReason.INVALID_PHONE_NUMBER, employee_number="00456", detail="mobile_phone"
                ),
                _notice(System.PERSONIO, NoticeReason.MISSING_FIELD, employee_number="00123", detail="office"),
            ]
        )
        assert report.data_errors == {
            System.PERSONIO: (
                "00123: Karrierestufe unbekannt",
                "00123: Standort fehlt",
                "00456: Mobilnummer ungültig",
                "Personio-ID 4711: keine MA-Nummer hinterlegt",
            ),
            System.WECLAPP: ("User-ID 1234: keine MA-Nummer hinterlegt",),
        }

    def test_deviations(self) -> None:
        report = build_report(
            sync_plan=_plan(
                EmployeePlan(
                    employee_number="00044",
                    deviations=(Deviation(system=TargetSystem.WECLAPP, fields=("vacation_days",)),),
                ),
                EmployeePlan(
                    employee_number="00006",
                    deviations=(
                        Deviation(system=TargetSystem.WECLAPP, fields=("weekly_hours",)),
                        Deviation(system=TargetSystem.DECIDALO, fields=("mobile_phone",)),
                    ),
                ),
            )
        )
        assert report.deviations == {TargetSystem.WECLAPP: ("00006", "00044"), TargetSystem.DECIDALO: ("00006",)}

    def test_nothing_to_report(self) -> None:
        report = build_report(sync_plan=_plan(), results=[])
        assert report.is_empty
        assert not build_report(aborted="kaputt").is_empty

    @pytest.mark.parametrize("reason", list(NoticeReason))
    def test_every_reason_has_a_text(self, reason: NoticeReason) -> None:
        assert reason in NOTICE_TEXTS

    def test_every_field_has_a_label(self) -> None:
        assert set(FIELD_LABELS) == set(EmployeeRecord.model_fields) - {"employee_number"}


class TestAbortReason:
    """
    Tests of the reason of an abort, in words for the HR management.
    """

    @pytest.mark.parametrize(
        ("problem", "expected"),
        [
            (ConfigurationProblem.MISSING_VARIABLES, "In der Konfiguration fehlen Einstellungen."),
            (ConfigurationProblem.INVALID_VARIABLES, "Die Konfiguration enthält ungültige Einstellungen."),
            (ConfigurationProblem.WECLAPP_SETUP_MISMATCH, "weclapp passt nicht zur Einrichtung von HR-Sync."),
            (ConfigurationProblem.TARGET_NOT_ALLOWED_IN_TEST_MODE, "Die Konfiguration passt nicht."),
        ],
    )
    def test_configuration(self, problem: ConfigurationProblem, expected: str) -> None:
        assert abort_reason(ConfigurationError(problem), executing=False) == f"{expected} Es wurde nichts angelegt."

    def test_error_of_a_system(self) -> None:
        # any error whose innermost frame of HR-Sync is in the adapter of weclapp
        with pytest.raises(ValueError) as error_info:
            weclapp_phone_number("not a number")
        assert abort_reason(error_info.value, executing=False) == (
            "weclapp war nicht erreichbar oder hat einen Fehler gemeldet. Es wurde nichts angelegt."
        )

    def test_unexpected_error_while_executing(self) -> None:
        reason = abort_reason(ValueError("Erika Musterfrau"), executing=True)
        assert reason == (
            "Ein unerwarteter Fehler ist aufgetreten. Ein Teil kann schon angelegt sein, Details stehen im Protokoll."
        )


class TestRenderReport:
    """
    Tests of the mail, in German and without personal data.
    """

    def test_mail(self) -> None:
        report = Report(
            created={"00965": ("weclapp", "Decidalo")},
            problems=("00974 (weclapp): nicht angelegt.",),
            data_errors={
                System.PERSONIO: ("00123: Karrierestufe unbekannt", "Personio-ID 4711: keine MA-Nummer hinterlegt"),
                System.WECLAPP: ("User-ID 1234: keine MA-Nummer hinterlegt",),
            },
            deviations={TargetSystem.WECLAPP: ("00006", "00044"), TargetSystem.DECIDALO: ("00013",)},
        )
        mail = render_report(report, targets=BOTH, today=TODAY, run_id=RUN_ID, dry_run=False)
        assert mail.subject == "HR-Sync 06.11.2026: 1 angelegt, 1 Problem, 3 Datenfehler"
        assert mail.text == (
            "HR-Sync hat Personio mit weclapp und Decidalo abgeglichen.\n"
            "\n"
            "ANGELEGT\n"
            "- 00965: weclapp, Decidalo\n"
            "\n"
            "PROBLEME BEIM ANLEGEN\n"
            "- 00974 (weclapp): nicht angelegt.\n"
            "\n"
            "DATENFEHLER\n"
            "Mitarbeiter mit Datenfehlern in Personio legt HR-Sync erst an, wenn der Fehler behoben ist.\n"
            "Personio\n"
            "- 00123: Karrierestufe unbekannt\n"
            "- Personio-ID 4711: keine MA-Nummer hinterlegt\n"
            "weclapp\n"
            "- User-ID 1234: keine MA-Nummer hinterlegt\n"
            "\n"
            "ABWEICHUNGEN\n"
            "Bestehende Mitarbeiter mit anderen Daten als in Personio. HR-Sync ändert sie nicht.\n"
            "- weclapp: 2 (00006, 00044)\n"
            "- Decidalo: 1 (00013)\n"
            "\n"
            "Lauf vom 06.11.2026, Lauf-ID 3f2a9c\n"
        )

    def test_only_sections_with_content(self) -> None:
        report = Report(deviations={TargetSystem.DECIDALO: ("00013", "00030")})
        mail = render_report(report, targets=(TargetSystem.DECIDALO,), today=TODAY, run_id=RUN_ID, dry_run=False)
        assert mail.subject == "HR-Sync 06.11.2026: 2 Abweichungen"
        assert mail.text.startswith("HR-Sync hat Personio mit Decidalo abgeglichen.\n\nABWEICHUNGEN\n")
        assert "ANGELEGT" not in mail.text

    def test_dry_run(self) -> None:
        report = Report(created={"00965": ("weclapp",), "00974": ("weclapp",)})
        mail = render_report(report, targets=BOTH, today=TODAY, run_id=RUN_ID, dry_run=True)
        assert mail.subject == "Probelauf: HR-Sync 06.11.2026: 2 angelegt"
        assert mail.text.startswith(
            "Probelauf: HR-Sync hat nichts geschrieben. So sähe der Bericht aus.\n\nWÜRDE ANGELEGT\n- 00965: weclapp\n"
        )

    def test_aborted(self) -> None:
        report = Report(aborted="In der Konfiguration fehlen Einstellungen. Es wurde nichts angelegt.")
        mail = render_report(report, targets=BOTH, today=TODAY, run_id=RUN_ID, dry_run=False)
        assert mail.subject == "HR-Sync 06.11.2026: Lauf abgebrochen"
        assert "ABGEBROCHEN\nIn der Konfiguration fehlen Einstellungen. Es wurde nichts angelegt.\n" in mail.text

    def test_no_personal_data(self) -> None:
        report = build_report(
            results=[_result("00965", TargetSystem.WECLAPP, Outcome.SUCCESS)],
            sync_plan=_plan(notices=(_notice(System.WECLAPP, NoticeReason.EMAIL_IN_USE, employee_number="00974"),)),
        )
        mail = render_report(report, targets=BOTH, today=TODAY, run_id=RUN_ID, dry_run=False)
        assert [value for value in ("Erika", "Musterfrau", "@example.com") if value in mail.text] == []


def _report_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "mailgun_api_key": SecretStr("mailgun-key"),
        "mailgun_domain": "mg.example.com",
        "hr_sync_report_from": "HR-Sync <hr-sync@mg.example.com>",
        "hr_sync_report_to": "hr@example.com, it@example.com",
    }
    return Settings.model_validate(values | changes)


class TestSendReport:
    """
    Tests of sending the mail through the HTTP API of Mailgun, with a mock transport.
    """

    def test_request(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "<1@mg.example.com>", "message": "Queued. Thank you."})

        send_report(Mail(subject="Betreff", text="Text"), _report_settings(), transport=httpx.MockTransport(handle))
        (request,) = requests
        assert (request.method, str(request.url)) == ("POST", "https://api.eu.mailgun.net/v3/mg.example.com/messages")
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"api:mailgun-key").decode()
        assert httpx.QueryParams(request.content.decode()) == httpx.QueryParams(
            {
                "from": "HR-Sync <hr-sync@mg.example.com>",
                "to": "hr@example.com, it@example.com",
                "subject": "Betreff",
                "text": "Text",
            }
        )

    def test_other_region(self) -> None:
        urls: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return httpx.Response(200, json={})

        settings = _report_settings(mailgun_base_url="https://api.mailgun.net/")
        send_report(Mail(subject="Betreff", text="Text"), settings, transport=httpx.MockTransport(handle))
        assert urls == ["https://api.mailgun.net/v3/mg.example.com/messages"]

    def test_error(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="Forbidden"))
        with pytest.raises(httpx.HTTPStatusError):
            send_report(Mail(subject="Betreff", text="Text"), _report_settings(), transport=transport)

    def test_incomplete_settings_are_a_programming_error(self) -> None:
        # check_settings reports missing variables before the run
        with pytest.raises(ValueError, match="Mailgun"):
            send_report(Mail(subject="Betreff", text="Text"), _report_settings(mailgun_domain=None))
