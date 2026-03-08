"""Tests for the ComposeRunner helper (nexus.cli.compose)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.cli.compose import (
    VALID_PROFILES,
    ComposeError,
    ComposeRunner,
    _ensure_docker,
    _find_compose_file,
    _validate_profiles,
)

# ---------------------------------------------------------------------------
# _find_compose_file
# ---------------------------------------------------------------------------


class TestFindComposeFile:
    def test_finds_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").touch()
        result = _find_compose_file(tmp_path)
        assert result == tmp_path / "docker-compose.yml"

    def test_finds_in_parent_dir(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").touch()
        child = tmp_path / "subdir"
        child.mkdir()
        result = _find_compose_file(child)
        assert result == tmp_path / "docker-compose.yml"

    def test_raises_when_not_found(self, tmp_path: Path) -> None:
        child = tmp_path / "isolated"
        child.mkdir()
        with pytest.raises(ComposeError, match="not found"):
            _find_compose_file(child)


# ---------------------------------------------------------------------------
# _validate_profiles
# ---------------------------------------------------------------------------


class TestValidateProfiles:
    def test_valid_profiles_pass_through(self) -> None:
        result = _validate_profiles(["server", "cache"])
        assert result == ["server", "cache"]

    def test_invalid_profile_raises(self) -> None:
        with pytest.raises(ComposeError, match="Unknown profile"):
            _validate_profiles(["server", "nonexistent"])

    def test_all_expands(self) -> None:
        result = _validate_profiles(["all"])
        assert set(result) == VALID_PROFILES - {"all", "test"}

    def test_empty_returns_empty(self) -> None:
        assert _validate_profiles([]) == []


# ---------------------------------------------------------------------------
# _ensure_docker
# ---------------------------------------------------------------------------


class TestEnsureDocker:
    @patch("nexus.cli.compose.subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        _ensure_docker()  # should not raise

    @patch("nexus.cli.compose.subprocess.run", side_effect=FileNotFoundError)
    def test_docker_not_installed(self, mock_run: MagicMock) -> None:
        with pytest.raises(ComposeError, match="not installed"):
            _ensure_docker()

    @patch("nexus.cli.compose.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10))
    def test_daemon_timeout(self, mock_run: MagicMock) -> None:
        with pytest.raises(ComposeError, match="not responding"):
            _ensure_docker()

    @patch("nexus.cli.compose.subprocess.run")
    def test_daemon_not_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Cannot connect to the Docker daemon",
        )
        with pytest.raises(ComposeError, match="not running"):
            _ensure_docker()


# ---------------------------------------------------------------------------
# ComposeRunner.run
# ---------------------------------------------------------------------------


class TestComposeRunnerRun:
    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run")
    def test_constructs_correct_command(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)

        mock_run.return_value = MagicMock(returncode=0)
        runner.run("up", "-d", profiles=["server"])

        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["docker", "compose"]
        assert "-f" in cmd
        assert "--profile" in cmd
        assert "server" in cmd
        assert "up" in cmd
        assert "-d" in cmd

    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run")
    def test_default_profiles(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)
        mock_run.return_value = MagicMock(returncode=0)

        runner.run("up")  # no profiles arg → defaults

        cmd = mock_run.call_args[0][0]
        assert cmd.count("--profile") == 3  # server, cache, events

    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5))
    def test_timeout_raises(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)
        with pytest.raises(ComposeError, match="timed out"):
            runner.run("up", timeout=5)

    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run", side_effect=FileNotFoundError)
    def test_docker_not_found_during_run(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)
        with pytest.raises(ComposeError, match="not installed"):
            runner.run("up")


# ---------------------------------------------------------------------------
# ComposeRunner.ps
# ---------------------------------------------------------------------------


class TestComposeRunnerPs:
    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run")
    def test_parses_json_output(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'{"Name": "nexus-server", "State": "running", "Health": "healthy"}\n'
            b'{"Name": "nexus-dragonfly", "State": "running", "Health": "healthy"}\n',
        )
        services = runner.ps()
        assert len(services) == 2
        assert services[0]["Name"] == "nexus-server"

    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run")
    def test_empty_output(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        assert runner.ps() == []

    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.run")
    def test_handles_invalid_json_lines(
        self,
        mock_run: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'{"Name": "ok"}\nnot-json\n',
        )
        services = runner.ps()
        assert len(services) == 1


# ---------------------------------------------------------------------------
# ComposeRunner.run_attached (signal forwarding)
# ---------------------------------------------------------------------------


class TestComposeRunnerRunAttached:
    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.Popen")
    def test_returns_exit_code(
        self,
        mock_popen: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)

        proc = MagicMock()
        proc.wait.return_value = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        assert runner.run_attached("up") == 0

    @patch("nexus.cli.compose._ensure_docker")
    @patch("nexus.cli.compose.subprocess.Popen")
    def test_nonzero_exit(
        self,
        mock_popen: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "docker-compose.yml").touch()
        runner = ComposeRunner(tmp_path)

        proc = MagicMock()
        proc.wait.return_value = 1
        proc.pid = 12345
        mock_popen.return_value = proc

        assert runner.run_attached("up") == 1
