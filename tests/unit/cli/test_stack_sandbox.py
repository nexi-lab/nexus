"""Tests for nexus up --profile sandbox shortcut (Issue #3786).

Verifies:
  1. sandbox profile with workspace invokes nexusd with correct args
  2. sandbox profile with workspace + hub-url + hub-token invokes nexusd with all args
  3. --workspace without --profile sandbox → exit(USAGE_ERROR)
  4. --hub-url without --profile sandbox → exit(USAGE_ERROR)
  5. --hub-token without --profile sandbox → exit(USAGE_ERROR)
  6. --profile sandbox with --hub-url but no --hub-token → exit(USAGE_ERROR)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.stack import up
from nexus.cli.exit_codes import ExitCode


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestSandboxShortcutHappyPath:
    def test_workspace_only_invokes_nexusd(self, runner: CliRunner, tmp_path: Path) -> None:
        """nexus up --profile sandbox --workspace /tmp/ws → nexusd with sandbox args."""
        ws = str(tmp_path / "workspace")
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == fake_nexusd
        assert "--profile" in call_args
        assert "sandbox" in call_args
        assert "--workspace" in call_args
        assert ws in call_args
        assert "--hub-url" not in call_args
        assert "--hub-token" not in call_args

    def test_all_sandbox_flags_invokes_nexusd(self, runner: CliRunner, tmp_path: Path) -> None:
        """nexus up --profile sandbox --workspace /tmp/ws --hub-url grpc://hub --hub-token tok."""
        ws = str(tmp_path / "workspace")
        hub_url = "grpc://hub.example.com:50051"
        hub_token = "secrettoken123"
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = runner.invoke(
                up,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    ws,
                    "--hub-url",
                    hub_url,
                    "--hub-token",
                    hub_token,
                ],
            )

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == fake_nexusd
        assert "--profile" in call_args
        assert "sandbox" in call_args
        assert "--workspace" in call_args
        assert ws in call_args
        assert "--hub-url" in call_args
        assert hub_url in call_args
        assert "--hub-token" in call_args
        assert hub_token in call_args

    def test_nexusd_fallback_to_module_when_not_in_path(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """When nexusd not in PATH, falls back to sys.executable -m nexus.daemon.main."""
        ws = str(tmp_path / "workspace")

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == sys.executable
        assert "-m" in call_args
        assert "nexus.daemon.main" in call_args
        assert "--profile" in call_args
        assert "sandbox" in call_args

    def test_hub_url_from_env(self, runner: CliRunner, tmp_path: Path) -> None:
        """NEXUS_HUB_URL + NEXUS_HUB_TOKEN env vars are picked up."""
        ws = str(tmp_path / "workspace")
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch(
                "os.environ",
                {
                    **__import__("os").environ,
                    "NEXUS_HUB_URL": "grpc://hub.env.example.com:50051",
                    "NEXUS_HUB_TOKEN": "envtoken",
                },
            ),
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])

        assert result.exit_code == 0, result.output
        call_args = mock_run.call_args[0][0]
        assert "--hub-url" in call_args
        assert "grpc://hub.env.example.com:50051" in call_args
        assert "--hub-token" in call_args
        assert "envtoken" in call_args


# ---------------------------------------------------------------------------
# Validation failure tests
# ---------------------------------------------------------------------------


class TestSandboxFlagValidation:
    def test_workspace_without_sandbox_profile_errors(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--workspace without --profile sandbox → exit(USAGE_ERROR)."""
        ws = str(tmp_path / "workspace")
        result = runner.invoke(up, ["--workspace", ws])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_hub_url_without_sandbox_profile_errors(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--hub-url without --profile sandbox → exit(USAGE_ERROR)."""
        result = runner.invoke(up, ["--hub-url", "grpc://hub.example.com:50051"])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_hub_token_without_sandbox_profile_errors(self, runner: CliRunner) -> None:
        """--hub-token without --profile sandbox → exit(USAGE_ERROR)."""
        result = runner.invoke(up, ["--hub-token", "mytoken"])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_hub_url_without_token_errors(self, runner: CliRunner, tmp_path: Path) -> None:
        """--profile sandbox --hub-url without --hub-token → exit(USAGE_ERROR)."""
        ws = str(tmp_path / "workspace")
        result = runner.invoke(
            up,
            [
                "--profile",
                "sandbox",
                "--workspace",
                ws,
                "--hub-url",
                "grpc://hub.example.com:50051",
            ],
        )
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "token" in result.output.lower()

    def test_hub_token_without_hub_url_is_valid(self, runner: CliRunner, tmp_path: Path) -> None:
        """--hub-token without --hub-url is allowed (token may be used for future URL)."""
        ws = str(tmp_path / "workspace")
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = runner.invoke(
                up,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    ws,
                    "--hub-token",
                    "tok",
                ],
            )

        # This is allowed — hub-token without hub-url is not an error
        assert result.exit_code == 0, result.output
