import runpy
import sys

import pytest

from hr_sync.cli import main


class TestCli:
    """
    Tests of the command line interface.
    """

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert capsys.readouterr().out.startswith("hr-sync ")

    def test_without_arguments(self) -> None:
        assert main([]) == 0

    def test_run_as_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["hr_sync"])
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module("hr_sync", run_name="__main__")
        assert exit_info.value.code == 0
