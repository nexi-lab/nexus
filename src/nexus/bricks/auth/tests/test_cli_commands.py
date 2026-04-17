from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from nexus.bricks.auth.cli_commands import auth
from nexus.bricks.auth.tests.helpers import build_unified_service_for_tests


def test_auth_group_importable() -> None:
    assert isinstance(auth, click.Group)
    assert auth.name == "auth"


def test_auth_group_has_expected_subcommands() -> None:
    expected = {"list", "test", "connect", "disconnect", "doctor", "pool", "migrate"}
    assert expected.issubset(set(auth.commands.keys()))


def test_list_shows_all_configured_services(monkeypatch, tmp_path: Path) -> None:
    service = build_unified_service_for_tests(tmp_path)
    # Seed one stored secret entry — same pattern used in tests/unit/cli/test_auth_cli.py
    service.connect_secret("s3", {"access_key_id": "AKIA_TEST", "secret_access_key": "secret"})

    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["list"])

    assert result.exit_code == 0, result.output
    # The seeded s3 entry should appear in the table output
    assert "s3" in result.output


def test_test_command_runs_for_configured_service(monkeypatch, tmp_path: Path) -> None:
    service = build_unified_service_for_tests(tmp_path)
    # Seed a stored s3 secret entry so auth test has something to find
    service.connect_secret("s3", {"access_key_id": "AKIA_TEST", "secret_access_key": "secret"})
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["test", "s3"])
    # Either success (0) or a well-formed failure (1) — we only care the command runs.
    assert result.exit_code in (0, 1)


def test_test_command_accepts_target_option(monkeypatch, tmp_path: Path) -> None:
    service = build_unified_service_for_tests(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["test", "gmail", "--target", "inbox"])
    # Pass if --target is accepted as a flag (the command may error because no
    # gmail auth is configured — that's fine; we're only asserting --target
    # is a valid option).
    assert "No such option" not in result.output


def test_connect_s3_guides_and_stores_native(monkeypatch, tmp_path: Path) -> None:
    service = build_unified_service_for_tests(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["connect", "s3"], input="native\n")
    assert result.exit_code == 0
    assert "s3" in result.output.lower()
