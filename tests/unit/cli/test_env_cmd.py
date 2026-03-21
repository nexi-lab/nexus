"""Tests for nexus.cli.commands.env_cmd — nexus env and nexus run."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.env_cmd import (
    _detect_shell,
    _format_dotenv,
    _format_shell,
    env_cmd,
    run,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Create a minimal nexus project with config and state."""
    data_dir = tmp_path / "nexus-data"
    data_dir.mkdir()

    config = {
        "preset": "shared",
        "data_dir": str(data_dir),
        "services": ["nexus", "postgres"],
        "ports": {"http": 2026, "grpc": 2028, "postgres": 5432},
        "api_key": "sk-test-key",
    }
    cfg_path = tmp_path / "nexus.yaml"
    cfg_path.write_text(yaml.dump(config))

    # Write state.json with resolved ports
    import json as _json

    state = {
        "version": 1,
        "ports": {"http": 3026, "grpc": 3028, "postgres": 5433},
        "api_key": "sk-runtime-key",
        "build_mode": "local",
        "image_used": "nexus:local-abc12345",
    }
    (data_dir / ".state.json").write_text(_json.dumps(state))

    return tmp_path


# ---------------------------------------------------------------------------
# Shell formatting
# ---------------------------------------------------------------------------


class TestFormatShell:
    def test_bash(self) -> None:
        env = {"NEXUS_URL": "http://localhost:2026", "NEXUS_API_KEY": "sk-abc"}
        output = _format_shell(env, "bash")
        assert "export NEXUS_API_KEY='sk-abc'" in output
        assert "export NEXUS_URL='http://localhost:2026'" in output

    def test_fish(self) -> None:
        env = {"NEXUS_URL": "http://localhost:2026"}
        output = _format_shell(env, "fish")
        assert "set -gx NEXUS_URL 'http://localhost:2026';" in output

    def test_powershell(self) -> None:
        env = {"NEXUS_URL": "http://localhost:2026"}
        output = _format_shell(env, "powershell")
        assert "$env:NEXUS_URL = 'http://localhost:2026'" in output


class TestFormatDotenv:
    def test_basic(self) -> None:
        env = {"NEXUS_URL": "http://localhost:2026", "NEXUS_API_KEY": "sk-abc"}
        output = _format_dotenv(env)
        assert "NEXUS_API_KEY=sk-abc" in output
        assert "NEXUS_URL=http://localhost:2026" in output
        assert "export" not in output


class TestDetectShell:
    def test_bash(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/bin/bash"}):
            assert _detect_shell() == "bash"

    def test_zsh(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/bin/zsh"}):
            assert _detect_shell() == "zsh"

    def test_fish(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/usr/bin/fish"}):
            assert _detect_shell() == "fish"

    def test_unknown_defaults_bash(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/usr/bin/custom-shell"}):
            assert _detect_shell() == "bash"


# ---------------------------------------------------------------------------
# nexus env command
# ---------------------------------------------------------------------------


class TestEnvCommand:
    def test_default_output(self, runner: CliRunner, project_dir: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(project_dir / "nexus.yaml"),),
        ):
            result = runner.invoke(env_cmd)
        assert result.exit_code == 0
        # State.json ports should win over config ports
        assert "3026" in result.output
        assert "sk-runtime-key" in result.output
        assert "export" in result.output

    def test_json_output(self, runner: CliRunner, project_dir: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(project_dir / "nexus.yaml"),),
        ):
            result = runner.invoke(env_cmd, ["--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["NEXUS_URL"] == "http://localhost:3026"
        assert data["NEXUS_API_KEY"] == "sk-runtime-key"

    def test_dotenv_output(self, runner: CliRunner, project_dir: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(project_dir / "nexus.yaml"),),
        ):
            result = runner.invoke(env_cmd, ["--dotenv"])
        assert result.exit_code == 0
        assert "export" not in result.output
        assert "NEXUS_URL=http://localhost:3026" in result.output

    def test_fish_shell(self, runner: CliRunner, project_dir: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(project_dir / "nexus.yaml"),),
        ):
            result = runner.invoke(env_cmd, ["--shell", "fish"])
        assert result.exit_code == 0
        assert "set -gx" in result.output

    def test_no_config_exits(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(tmp_path / "nope.yaml"),),
        ):
            result = runner.invoke(env_cmd)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# nexus run command
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_run_echo(self, runner: CliRunner, project_dir: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(project_dir / "nexus.yaml"),),
        ):
            result = runner.invoke(run, ["echo", "hello"])
        assert result.exit_code == 0

    def test_run_missing_command(self, runner: CliRunner, project_dir: Path) -> None:
        with patch(
            "nexus.cli.state.CONFIG_SEARCH_PATHS",
            (str(project_dir / "nexus.yaml"),),
        ):
            result = runner.invoke(run, ["nonexistent-command-xyz"])
        assert result.exit_code == 127
