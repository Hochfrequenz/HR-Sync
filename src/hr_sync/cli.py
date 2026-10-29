"""
The command line interface of HR-Sync.
"""

import argparse
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import get_args

from hr_sync.config import load_settings
from hr_sync.events import LogFormat, configure_logging
from hr_sync.model import TargetSystem
from hr_sync.sync import RunOptions, abort, run

_EXIT_CODES = """\
exit codes:
  0  success, also with an empty plan or with notices
  1  at least one operation failed or was skipped
  2  aborted, e.g. because of the configuration or an error while loading
"""


def _employee_number(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("the employee number must not be empty")
    return value.strip()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hr-sync",
        description="Rolls out the master data of new employees from Personio to weclapp and Decidalo.",
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="load and plan, but don't write to any target system")
    parser.add_argument(
        "--only",
        action="extend",
        nargs="+",
        type=_employee_number,
        metavar="EMPLOYEE_NUMBER",
        help="restrict the plan to these employee numbers; all employees are loaded anyway, to find data problems",
    )
    parser.add_argument(
        "--target",
        action="extend",
        nargs="+",
        choices=[target.value for target in TargetSystem],
        help="the target systems (default: all); the others are neither loaded nor written",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="read the configuration from this file instead of .env in the current directory",
    )
    parser.add_argument(
        "--log-format",
        choices=get_args(LogFormat),
        help="the format of the events (default: HR_SYNC_LOG_FORMAT, otherwise pretty)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('hochfrequenz-hr-sync')}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Runs HR-Sync with the given command line arguments and returns the exit code (see :class:`hr_sync.sync.ExitCode`).
    """
    args = _build_parser().parse_args(argv)
    options = RunOptions(
        targets=tuple(target for target in TargetSystem if args.target is None or target in args.target),
        only=frozenset(args.only) if args.only is not None else None,
        dry_run=args.dry_run,
    )
    try:
        settings = load_settings(args.env_file)
    except Exception as error:  # as in run(): any error ends the run with an event, e.g. an unreadable .env file
        # the log format of the settings is unknown, so the events use the one from the command line or the default
        configure_logging(args.log_format or "pretty")
        return abort(options, error)
    configure_logging(args.log_format or settings.hr_sync_log_format)
    return run(settings, options)
