"""
The command line interface of HR-Sync.
"""

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hr-sync",
        description="Rolls out the master data of new employees from Personio to weclapp and Decidalo.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('hr-sync')}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Runs HR-Sync with the given command line arguments and returns the exit code.
    """
    _build_parser().parse_args(argv)
    return 0
