"""Ensure both entry points (nexus auth + nexus-fs auth) expose identical behavior
after the Phase 4 unification."""

from __future__ import annotations

from click.testing import CliRunner

from nexus.cli.commands.auth_cli import auth as cli_auth
from nexus.fs._auth_cli import auth as fs_auth


def test_same_subcommands_registered() -> None:
    """Both entry points expose the same set of subcommands.

    The two groups are separate Click objects (fs_auth is defined in the fs
    entry point so nexus-fs can still load without the `nexus.bricks` package
    in the slim wheel), but when the full package is installed they share
    identical subcommand references.
    """
    assert set(cli_auth.commands.keys()) == set(fs_auth.commands.keys())


def test_same_subcommand_objects() -> None:
    """Subcommand objects are the literal same references — no handler drift."""
    for name, cli_cmd in cli_auth.commands.items():
        assert fs_auth.commands[name] is cli_cmd, (
            f"subcommand {name!r} has drifted between nexus auth and nexus-fs auth"
        )


def test_list_parity(monkeypatch, tmp_path):
    from nexus.bricks.auth.tests.helpers import build_unified_service_for_tests

    service = build_unified_service_for_tests(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()
    cli_result = runner.invoke(cli_auth, ["list"])
    fs_result = runner.invoke(fs_auth, ["list"])

    assert cli_result.output == fs_result.output
    assert cli_result.exit_code == fs_result.exit_code


def test_doctor_parity(monkeypatch, tmp_path):
    from nexus.bricks.auth.tests.helpers import build_unified_service_for_tests

    service = build_unified_service_for_tests(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()
    cli_result = runner.invoke(cli_auth, ["doctor"])
    fs_result = runner.invoke(fs_auth, ["doctor"])

    assert cli_result.output == fs_result.output
    assert cli_result.exit_code == fs_result.exit_code
