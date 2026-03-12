"""Regression tests for the documented local CLI quickstart."""

from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from nexus.cli.commands import LazyCommandGroup, register_all_commands
from nexus.cli.main import main
from nexus.raft import zone_manager


def test_local_cli_quickstart_persists_across_invocations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The local CLI quickstart should work from a source checkout."""

    def _raise_missing_full_build(*args, **kwargs):
        raise RuntimeError(
            "ZoneManager requires PyO3 build with --features full. "
            "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
        )

    monkeypatch.setattr(zone_manager, "ZoneManager", _raise_missing_full_build)

    runner = CliRunner()
    workspace = tmp_path / "cli-demo"
    home_dir = tmp_path / "home"
    config_dir = home_dir / ".nexus"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "\n".join(
            [
                "current-profile: ci-remote",
                "profiles:",
                "  ci-remote:",
                "    url: http://127.0.0.1:65535",
                "    api-key: test-key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = {
        "HOME": str(home_dir),
        "NEXUS_DATA_DIR": str(workspace / "nexus-data"),
        "NEXUS_URL": "http://127.0.0.1:65535",
        "NEXUS_DATABASE_URL": "sqlite:///" + str(tmp_path / "ambient.db"),
        "NEXUS_METASTORE_PATH": str(tmp_path / "ambient-metastore"),
        "NEXUS_RECORD_STORE_PATH": str(tmp_path / "ambient-record-store.db"),
    }

    init_result = runner.invoke(main, ["init", str(workspace)], env=env)
    assert init_result.exit_code == 0, init_result.output

    write_result = runner.invoke(
        main,
        ["write", "/workspace/hello.txt", "hello from cli"],
        env=env,
    )
    assert write_result.exit_code == 0, write_result.output

    cat_result = runner.invoke(main, ["cat", "/workspace/hello.txt", "--json"], env=env)
    assert cat_result.exit_code == 0, cat_result.output
    assert "hello from cli" in cat_result.output

    ls_result = runner.invoke(main, ["ls", "/workspace", "--json"], env=env)
    assert ls_result.exit_code == 0, ls_result.output
    assert "/workspace/hello.txt" in ls_result.output


def test_cli_help_registers_local_quickstart_commands() -> None:
    """Quickstart commands should be present in the top-level CLI help."""

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])

    assert result.exit_code == 0, result.output
    for command_name in ("init", "write", "cat", "ls"):
        assert command_name in result.output


def test_lazy_registration_defers_command_module_import(monkeypatch) -> None:
    """Top-level CLI registration should not import every command eagerly."""
    imported: list[str] = []
    real_import = __import__("importlib").import_module

    def _spy(name: str, package: str | None = None):
        imported.append(name)
        return real_import(name, package)

    monkeypatch.setattr("nexus.cli.commands.importlib.import_module", _spy)

    cli = LazyCommandGroup(name="nexus")
    register_all_commands(cli)
    assert imported == []

    command = cli.get_command(click.Context(cli), "ls")

    assert command is not None
    assert "nexus.cli.commands.directory" in imported
    assert "nexus.cli.commands.oauth" not in imported


def test_lazy_registration_supports_hyphenated_add_command_groups() -> None:
    """Lazy top-level registration should preserve hyphenated Click command names."""
    command = main.get_command(click.Context(main), "secrets-audit")

    assert command is not None
    assert command.name == "secrets-audit"
