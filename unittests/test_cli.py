import json
import runpy
import sys
from pathlib import Path
from unittest.mock import ANY

import pytest

from hr_sync.cli import main
from hr_sync.config import Settings
from hr_sync.events import LogFormat
from hr_sync.model import TargetSystem
from hr_sync.sync import ExitCode, RunOptions, Systems
from unittests.fakes import FakeDecidalo, FakePersonio, personio_employment, personio_person

_CREDENTIALS = """\
PERSONIO_CLIENT_ID=papi-id
PERSONIO_CLIENT_SECRET=papi-secret
DECIDALO_API_KEY=decidalo-key
"""


class _Runs:
    """Replaces the run of the sync and records its settings and options."""

    def __init__(self) -> None:
        self.calls: list[tuple[Settings, RunOptions]] = []

    def __call__(self, settings: Settings, options: RunOptions) -> ExitCode:
        self.calls.append((settings, options))
        return ExitCode.SUCCESS


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> _Runs:
    replaced = _Runs()
    monkeypatch.setattr("hr_sync.cli.run", replaced)
    return replaced


@pytest.fixture
def log_formats(monkeypatch: pytest.MonkeyPatch) -> list[LogFormat]:
    """Replaces the setup of the logging and records the log formats."""
    formats: list[LogFormat] = []
    monkeypatch.setattr("hr_sync.cli.configure_logging", formats.append)
    return formats


def _json_events(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines()]


class TestOptions:
    """
    Tests of the command line options.
    """

    @pytest.mark.usefixtures("log_formats")
    def test_defaults(self, runs: _Runs) -> None:
        assert main([]) == ExitCode.SUCCESS
        ((_, options),) = runs.calls
        assert options == RunOptions(targets=(TargetSystem.WECLAPP, TargetSystem.DECIDALO), only=None, dry_run=False)

    @pytest.mark.usefixtures("log_formats")
    def test_options(self, runs: _Runs) -> None:
        argv = ["--dry-run", "--only", "00965", " 00200 ", "--only", "00300", "--target", "decidalo"]
        assert main([*argv, "--max-employees", "1"]) == 0
        ((_, options),) = runs.calls
        assert options == RunOptions(
            targets=(TargetSystem.DECIDALO,),
            only=frozenset({"00965", "00200", "00300"}),
            max_employees=1,
            dry_run=True,
        )

    @pytest.mark.usefixtures("log_formats")
    def test_targets_are_executed_in_their_order(self, runs: _Runs) -> None:
        main(["--target", "decidalo", "weclapp", "--target", "decidalo"])
        ((_, options),) = runs.calls
        assert options.targets == (TargetSystem.WECLAPP, TargetSystem.DECIDALO)

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--only", " "], id="empty employee number"),
            pytest.param(["--max-employees", "0"], id="no employee"),
            pytest.param(["--max-employees", "-1"], id="negative number of employees"),
            pytest.param(["--max-employees", "one"], id="number of employees is not a number"),
            pytest.param(["--target", "sap"], id="unknown target"),
            pytest.param(["--log-format", "xml"], id="unknown log format"),
        ],
    )
    def test_invalid_options(self, argv: list[str], runs: _Runs) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == ExitCode.ABORTED
        assert runs.calls == []

    @pytest.mark.usefixtures("log_formats")
    def test_env_file(self, runs: _Runs, tmp_path: Path) -> None:
        env_file = tmp_path / "hr-sync.env"
        env_file.write_text(_CREDENTIALS, encoding="utf-8")
        main(["--env-file", str(env_file)])
        ((settings, _),) = runs.calls
        assert settings.decidalo_api_key is not None
        assert settings.decidalo_api_key.get_secret_value() == "decidalo-key"

    @pytest.mark.usefixtures("runs")
    def test_log_format_from_the_settings(self, log_formats: list[LogFormat]) -> None:
        Path(".env").write_text("HR_SYNC_LOG_FORMAT=json\n", encoding="utf-8")
        main([])
        assert log_formats == ["json"]

    @pytest.mark.usefixtures("runs")
    def test_log_format_from_the_command_line(self, log_formats: list[LogFormat]) -> None:
        Path(".env").write_text("HR_SYNC_LOG_FORMAT=json\n", encoding="utf-8")
        main(["--log-format", "pretty"])
        assert log_formats == ["pretty"]

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert capsys.readouterr().out.startswith("hr-sync ")

    def test_help_names_the_exit_codes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "exit codes:" in capsys.readouterr().out

    def test_run_as_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["hr_sync", "--version"])
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module("hr_sync", run_name="__main__")
        assert exit_info.value.code == 0


@pytest.mark.usefixtures("restore_logging")
class TestMain:
    """
    Tests of whole runs from the command line, with the events on stdout.
    """

    def test_run(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        Path(".env").write_text(_CREDENTIALS, encoding="utf-8")
        decidalo = FakeDecidalo()
        personio = FakePersonio([(personio_person(), [personio_employment()])])
        monkeypatch.setattr("hr_sync.sync.connect", lambda settings, targets: Systems(personio, decidalo))
        assert main(["--target", "decidalo", "--log-format", "json"]) == ExitCode.SUCCESS
        output = capsys.readouterr().out
        assert [event["event"] for event in _json_events(output)] == [
            "sync.load",
            "sync.load",
            "sync.plan",
            "sync.operation",
            "sync.run",
        ]
        assert len(decidalo.batches) == 1
        assert "Erika" not in output
        assert "example.com" not in output
        assert "papi-secret" not in output

    def test_invalid_settings(self, capsys: pytest.CaptureFixture[str]) -> None:
        Path(".env").write_text("HR_SYNC_LOG_FORMAT=xml\nDECIDALO_HAS_LOGIN=secret-value\n", encoding="utf-8")
        assert main(["--log-format", "json"]) == ExitCode.ABORTED
        output = capsys.readouterr().out
        (event,) = _json_events(output)
        assert event["event"] == "sync.run"
        assert event["level"] == "ERROR"
        assert isinstance(event["error"], dict)
        assert event["error"]["problem"] == "invalid_variables"
        assert event["error"]["variables"] == ["DECIDALO_HAS_LOGIN", "HR_SYNC_LOG_FORMAT"]
        assert event["exit_code"] == ExitCode.ABORTED
        assert isinstance(event["run_id"], str)
        assert "secret-value" not in output

    def test_invalid_settings_are_logged_pretty_by_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        Path(".env").write_text("HR_SYNC_LOG_FORMAT=xml\n", encoding="utf-8")
        assert main([]) == ExitCode.ABORTED
        output = capsys.readouterr().out
        assert "| ERROR   | sync.run " in output
        assert "error.problem=invalid_variables" in output

    def test_unreadable_env_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        # not UTF-8: reading it fails before the settings are validated
        Path(".env").write_bytes("PERSONIO_APP_ID=Müller\n".encode("cp1252"))
        assert main(["--log-format", "json"]) == ExitCode.ABORTED
        output = capsys.readouterr().out
        (event,) = _json_events(output)
        assert event["event"] == "sync.run"
        assert event["error"] == {
            "type": "UnicodeDecodeError",
            "module": "hr_sync.config",
            "function": "load_settings",
            "line": ANY,
        }
        assert event["exit_code"] == ExitCode.ABORTED

    def test_env_file_not_found(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        assert main(["--env-file", str(tmp_path / "missing.env"), "--log-format", "json"]) == ExitCode.ABORTED
        (event,) = _json_events(capsys.readouterr().out)
        assert isinstance(event["error"], dict)
        assert event["error"]["problem"] == "env_file_not_found"

    def test_weclapp_needs_its_variables(self, capsys: pytest.CaptureFixture[str]) -> None:
        # without --target, weclapp is a target
        Path(".env").write_text(_CREDENTIALS, encoding="utf-8")
        assert main(["--dry-run", "--log-format", "json"]) == ExitCode.ABORTED
        (event,) = _json_events(capsys.readouterr().out)
        assert isinstance(event["error"], dict)
        assert event["error"]["problem"] == "missing_variables"
        assert event["error"]["variables"] == [
            "WECLAPP_API_TOKEN",
            "WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID",
            "WECLAPP_TENANT",
            "WECLAPP_BASE_URL",
        ]
