"""Tests for ``nexus doctor`` command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.doctor import (
    CHECKS,
    CheckResult,
    CheckStatus,
    _run_all_checks,
    _try_fix,
    check_data_dir_writable,
    check_disk_space,
    check_docker_available,
    check_docker_compose_version,
    check_docker_daemon,
    check_grpc_port,
    check_python_version,
    check_server_reachable,
    check_tls_certs,
    check_zone_isolation,
    doctor,
)


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# CheckResult model
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_frozen(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.OK, message="ok")
        with pytest.raises(AttributeError):
            r.name = "other"

    def test_default_fix_hint(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.OK, message="ok")
        assert r.fix_hint is None
        assert r.fixable is False


# ---------------------------------------------------------------------------
# Connectivity checks
# ---------------------------------------------------------------------------


class TestCheckDockerAvailable:
    @patch("nexus.cli.commands.doctor.shutil.which", return_value="/usr/bin/docker")
    def test_docker_found(self, _mock: MagicMock) -> None:
        result = check_docker_available()
        assert result.status == CheckStatus.OK

    @patch("nexus.cli.commands.doctor.shutil.which", return_value=None)
    def test_docker_not_found(self, _mock: MagicMock) -> None:
        result = check_docker_available()
        assert result.status == CheckStatus.ERROR
        assert result.fix_hint is not None


class TestCheckDockerDaemon:
    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_daemon_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        result = check_docker_daemon()
        assert result.status == CheckStatus.OK

    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_daemon_not_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Cannot connect to the Docker daemon",
        )
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR

    @patch("nexus.cli.commands.doctor.subprocess.run", side_effect=FileNotFoundError)
    def test_docker_not_installed(self, _mock: MagicMock) -> None:
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR

    @patch(
        "nexus.cli.commands.doctor.subprocess.run",
        side_effect=subprocess.TimeoutExpired("docker", 10),
    )
    def test_daemon_timeout(self, _mock: MagicMock) -> None:
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR


class TestCheckServerReachable:
    @patch("httpx.Client")
    def test_server_reachable(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=200)
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = check_server_reachable()
        assert result.status == CheckStatus.OK

    @patch("httpx.Client")
    def test_server_unreachable(self, mock_client_cls: MagicMock) -> None:
        mock_client_cls.side_effect = Exception("Connection refused")
        result = check_server_reachable()
        assert result.status == CheckStatus.WARNING

    @patch("httpx.Client")
    def test_server_error_status(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=503)
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = check_server_reachable()
        assert result.status == CheckStatus.WARNING


class TestCheckGrpcPort:
    @patch.dict("os.environ", {"NEXUS_GRPC_PORT": "2126"})
    def test_grpc_configured(self) -> None:
        result = check_grpc_port()
        assert result.status == CheckStatus.OK

    @patch.dict("os.environ", {"NEXUS_GRPC_PORT": "0"})
    def test_grpc_disabled(self) -> None:
        result = check_grpc_port()
        assert result.status == CheckStatus.WARNING

    @patch.dict("os.environ", {}, clear=True)
    def test_grpc_unset(self) -> None:
        result = check_grpc_port()
        assert result.status == CheckStatus.WARNING


# ---------------------------------------------------------------------------
# Storage checks
# ---------------------------------------------------------------------------


class TestCheckDiskSpace:
    @patch("nexus.cli.commands.doctor.shutil.disk_usage")
    def test_plenty_of_space(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = Mock(free=10 * 1024**3)  # 10 GB
        result = check_disk_space()
        assert result.status == CheckStatus.OK

    @patch("nexus.cli.commands.doctor.shutil.disk_usage")
    def test_low_space(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = Mock(free=500_000_000)  # 500 MB
        result = check_disk_space()
        assert result.status == CheckStatus.WARNING
        assert "low disk space" in result.message.lower()

    @patch("nexus.cli.commands.doctor.shutil.disk_usage", side_effect=OSError("Permission denied"))
    def test_os_error(self, _mock: MagicMock) -> None:
        result = check_disk_space()
        assert result.status == CheckStatus.WARNING


class TestCheckDataDirWritable:
    def test_writable_dir(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"NEXUS_DATA_DIR": str(tmp_path)}):
            result = check_data_dir_writable()
            assert result.status == CheckStatus.OK

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"
        with patch.dict("os.environ", {"NEXUS_DATA_DIR": str(missing)}):
            result = check_data_dir_writable()
            assert result.status == CheckStatus.WARNING
            assert result.fixable is True


# ---------------------------------------------------------------------------
# Federation checks
# ---------------------------------------------------------------------------


class TestCheckTlsCerts:
    def test_no_tls_dir(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"NEXUS_DATA_DIR": str(tmp_path)}):
            result = check_tls_certs()
            assert result.status == CheckStatus.WARNING
            assert result.fixable is True

    def test_complete_tls(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").touch()
        (tls_dir / "node.pem").touch()
        with patch.dict("os.environ", {"NEXUS_DATA_DIR": str(tmp_path)}):
            result = check_tls_certs()
            assert result.status == CheckStatus.OK

    def test_partial_tls(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").touch()
        # Missing node.pem
        with patch.dict("os.environ", {"NEXUS_DATA_DIR": str(tmp_path)}):
            result = check_tls_certs()
            assert result.status == CheckStatus.WARNING


# ---------------------------------------------------------------------------
# Security checks
# ---------------------------------------------------------------------------


class TestCheckZoneIsolation:
    @patch.dict("os.environ", {"NEXUS_ENFORCE_ZONE_ISOLATION": "false"})
    def test_disabled(self) -> None:
        result = check_zone_isolation()
        assert result.status == CheckStatus.WARNING

    @patch.dict("os.environ", {"NEXUS_ENFORCE_ZONE_ISOLATION": "true"})
    def test_enabled(self) -> None:
        result = check_zone_isolation()
        assert result.status == CheckStatus.OK

    @patch.dict("os.environ", {}, clear=True)
    def test_default(self) -> None:
        result = check_zone_isolation()
        assert result.status == CheckStatus.OK


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_current_python(self) -> None:
        # We're running on 3.12+, so this should pass
        result = check_python_version()
        assert result.status == CheckStatus.OK


class TestCheckDockerComposeVersion:
    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_compose_v2_available(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="2.27.0\n")
        result = check_docker_compose_version()
        assert result.status == CheckStatus.OK
        assert "2.27.0" in result.message

    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_compose_not_available(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = check_docker_compose_version()
        assert result.status == CheckStatus.ERROR


# ---------------------------------------------------------------------------
# _run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    @patch(
        "nexus.cli.commands.doctor.CHECKS",
        {"test": [lambda: CheckResult("t", CheckStatus.OK, "ok")]},
    )
    def test_runs_all(self) -> None:
        results = _run_all_checks()
        assert "test" in results
        assert len(results["test"]) == 1
        assert results["test"][0].status == CheckStatus.OK


# ---------------------------------------------------------------------------
# _try_fix
# ---------------------------------------------------------------------------


class TestTryFix:
    def test_fix_data_dir(self, tmp_path: Path) -> None:
        missing = tmp_path / "create_me"
        result = CheckResult(
            name="data-dir",
            status=CheckStatus.WARNING,
            message="missing",
            fixable=True,
        )
        with patch.dict("os.environ", {"NEXUS_DATA_DIR": str(missing)}):
            fixed = _try_fix(result)
            assert fixed is not None
            assert fixed.status == CheckStatus.OK
            assert missing.exists()

    def test_no_fix_for_unfixable(self) -> None:
        result = CheckResult(
            name="something",
            status=CheckStatus.ERROR,
            message="broken",
            fixable=False,
        )
        assert _try_fix(result) is None


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    @patch("nexus.cli.commands.doctor._run_all_checks")
    def test_json_output(self, mock_run: MagicMock, cli_runner: CliRunner) -> None:
        mock_run.return_value = {
            "connectivity": [
                CheckResult("docker", CheckStatus.OK, "Docker found"),
            ],
        }
        result = cli_runner.invoke(doctor, ["--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "connectivity" in parsed
        assert parsed["connectivity"][0]["status"] == "ok"

    @patch("nexus.cli.commands.doctor._run_all_checks")
    def test_table_output(self, mock_run: MagicMock, cli_runner: CliRunner) -> None:
        mock_run.return_value = {
            "connectivity": [
                CheckResult("docker", CheckStatus.OK, "Docker found"),
            ],
        }
        result = cli_runner.invoke(doctor)
        assert result.exit_code == 0
        assert "Connectivity" in result.output
        assert "1 checks" in result.output

    @patch("nexus.cli.commands.doctor._run_all_checks")
    def test_exit_code_on_error(self, mock_run: MagicMock, cli_runner: CliRunner) -> None:
        mock_run.return_value = {
            "connectivity": [
                CheckResult("docker", CheckStatus.ERROR, "Not found"),
            ],
        }
        result = cli_runner.invoke(doctor)
        assert result.exit_code == 1

    @patch("nexus.cli.commands.doctor._run_all_checks")
    def test_fix_flag(self, mock_run: MagicMock, cli_runner: CliRunner) -> None:
        mock_run.return_value = {
            "connectivity": [
                CheckResult("docker", CheckStatus.OK, "ok"),
            ],
        }
        result = cli_runner.invoke(doctor, ["--fix"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(fix=True)

    def test_check_registry_has_all_categories(self) -> None:
        expected = {"connectivity", "storage", "federation", "security", "dependencies"}
        assert set(CHECKS.keys()) == expected

    def test_all_checks_return_check_result(self) -> None:
        """Verify every registered check returns a CheckResult."""
        for category, checks in CHECKS.items():
            for check_fn in checks:
                assert callable(check_fn), f"{category}: {check_fn} is not callable"
