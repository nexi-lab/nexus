"""Tests for infrastructure lifecycle commands (up, down, logs)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.infra import down, logs, up


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()


def _mock_runner(returncode: int = 0, attached_code: int = 0) -> MagicMock:
    """Create a pre-configured mock ComposeRunner."""
    runner = MagicMock()
    runner.run.return_value = MagicMock(returncode=returncode)
    runner.run_attached.return_value = attached_code
    return runner


# ---------------------------------------------------------------------------
# nexus up
# ---------------------------------------------------------------------------


class TestUp:
    @patch("nexus.cli.compose.ComposeRunner")
    def test_up_default_profiles(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(up)
        assert result.exit_code == 0
        assert "Starting" in result.output

        # Verify run was called
        mock_cls.return_value.run.assert_called_once()
        args = mock_cls.return_value.run.call_args[0]
        assert "up" in args

    @patch("nexus.cli.compose.ComposeRunner")
    def test_up_custom_profiles(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(up, ["--profile", "server", "--profile", "mcp"])
        assert result.exit_code == 0
        assert "server" in result.output
        assert "mcp" in result.output

    @patch("nexus.cli.compose.ComposeRunner")
    def test_up_with_build(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(up, ["--build"])
        assert result.exit_code == 0
        args = mock_cls.return_value.run.call_args[0]
        assert "--build" in args

    @patch("nexus.cli.compose.ComposeRunner")
    def test_up_failure(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner(returncode=1)

        result = cli_runner.invoke(up)
        assert result.exit_code != 0
        assert "Failed" in result.output

    @patch("nexus.cli.compose.ComposeRunner")
    def test_up_no_detach(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(up, ["--no-detach"])
        assert result.exit_code == 0
        mock_cls.return_value.run_attached.assert_called_once()

    @patch("nexus.cli.compose.ComposeRunner")
    def test_up_compose_error(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        from nexus.cli.compose import ComposeError

        mock_cls.side_effect = ComposeError("Docker not installed")

        result = cli_runner.invoke(up)
        assert result.exit_code != 0
        assert "Docker not installed" in result.output


# ---------------------------------------------------------------------------
# nexus down
# ---------------------------------------------------------------------------


class TestDown:
    @patch("nexus.cli.compose.ComposeRunner")
    def test_down_basic(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(down)
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    @patch("nexus.cli.compose.ComposeRunner")
    def test_down_with_volumes(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(down, ["--volumes"])
        assert result.exit_code == 0
        args = mock_cls.return_value.run.call_args[0]
        assert "--volumes" in args

    @patch("nexus.cli.compose.ComposeRunner")
    def test_down_failure(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner(returncode=1)

        result = cli_runner.invoke(down)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# nexus logs
# ---------------------------------------------------------------------------


class TestLogs:
    @patch("nexus.cli.compose.ComposeRunner")
    def test_logs_default(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(logs)
        assert result.exit_code == 0
        mock_cls.return_value.run_attached.assert_called_once()
        args = mock_cls.return_value.run_attached.call_args[0]
        assert "logs" in args
        assert "--follow" in args

    @patch("nexus.cli.compose.ComposeRunner")
    def test_logs_no_follow(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(logs, ["--no-follow"])
        assert result.exit_code == 0
        args = mock_cls.return_value.run_attached.call_args[0]
        assert "--follow" not in args

    @patch("nexus.cli.compose.ComposeRunner")
    def test_logs_specific_service(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(logs, ["nexus-server"])
        assert result.exit_code == 0
        args = mock_cls.return_value.run_attached.call_args[0]
        assert "nexus-server" in args

    @patch("nexus.cli.compose.ComposeRunner")
    def test_logs_custom_tail(self, mock_cls: MagicMock, cli_runner: CliRunner) -> None:
        mock_cls.return_value = _mock_runner()

        result = cli_runner.invoke(logs, ["--tail", "50"])
        assert result.exit_code == 0
        args = mock_cls.return_value.run_attached.call_args[0]
        assert "50" in args
