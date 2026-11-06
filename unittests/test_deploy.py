"""
Tests of the template for the operation on a server, deploy/compose.yaml (section "Operation" in the README).
"""

from pathlib import Path
from typing import get_args

from pydantic import SecretStr

from hr_sync.config import Settings

COMPOSE_FILE = Path(__file__).parent.parent / "deploy" / "compose.yaml"


def _secret_variables() -> list[str]:
    return sorted(
        name.upper() for name, info in Settings.model_fields.items() if SecretStr in get_args(info.annotation)
    )


def test_compose_passes_every_secret() -> None:
    """
    In a Git stack, Dockhand passes the variables marked as secret only to docker compose, never in a file, so the
    template must name each one under ``environment``. Empty values count as unset (``env_ignore_empty``).
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    secrets = _secret_variables()
    assert "WECLAPP_API_TOKEN" in secrets
    assert [name for name in secrets if f"{name}: ${{{name}:-}}" not in text] == []


def test_compose_reads_both_env_files() -> None:
    """
    A copy of the template reads the .env next to it, a Git stack in Dockhand the file .env.dockhand; neither exists in
    the other case, so both are optional.
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    for env_file in (".env", ".env.dockhand"):
        assert f"- path: {env_file}\n        required: false\n" in text
