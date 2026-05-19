"""Tests for ``nexus doctor`` diagnostic checks.

Covers all public check functions, the CheckResult/CheckStatus model,
_run_all_checks_async, and the _try_fix auto-repair logic.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.doctor import (
    CHECKS,
    CheckResult,
    CheckStatus,
    _run_all_checks_async,
    _try_fix,
    check_data_dir_writable,
    check_database_url,
    check_disk_space,
    check_docker_available,
    check_docker_compose_version,
    check_docker_daemon,
    check_grpc_port,
    check_pgvector,
    check_python_version,
    check_server_reachable,
    check_tls_certs,
    check_tls_expiry,
    check_zone_isolation,
    doctor,
    doctor_remote,
)


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# CheckResult model and CheckStatus enum
# ---------------------------------------------------------------------------


class TestCheckStatusEnum:
    def test_values(self) -> None:
        assert CheckStatus.OK.value == "ok"
        assert CheckStatus.WARNING.value == "warning"
        assert CheckStatus.ERROR.value == "error"

    def test_all_members(self) -> None:
        assert set(CheckStatus) == {CheckStatus.OK, CheckStatus.WARNING, CheckStatus.ERROR}


class TestCheckResult:
    def test_frozen(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.OK, message="ok")
        with pytest.raises(AttributeError):
            r.name = "other"

    def test_default_fix_hint(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.OK, message="ok")
        assert r.fix_hint is None
        assert r.fixable is False

    def test_custom_fields(self) -> None:
        r = CheckResult(
            name="example",
            status=CheckStatus.WARNING,
            message="low",
            fix_hint="do something",
            fixable=True,
        )
        assert r.name == "example"
        assert r.status == CheckStatus.WARNING
        assert r.message == "low"
        assert r.fix_hint == "do something"
        assert r.fixable is True

    def test_equality(self) -> None:
        a = CheckResult(name="x", status=CheckStatus.OK, message="ok")
        b = CheckResult(name="x", status=CheckStatus.OK, message="ok")
        assert a == b


# ---------------------------------------------------------------------------
# Connectivity checks
# ---------------------------------------------------------------------------


class TestCheckDockerAvailable:
    @patch("nexus.cli.commands.doctor.shutil.which", return_value="/usr/bin/docker")
    def test_docker_found(self, _mock: MagicMock) -> None:
        result = check_docker_available()
        assert result.status == CheckStatus.OK
        assert result.name == "docker"

    @patch("nexus.cli.commands.doctor.shutil.which", return_value=None)
    def test_docker_not_found(self, _mock: MagicMock) -> None:
        result = check_docker_available()
        assert result.status == CheckStatus.ERROR
        assert result.fix_hint is not None
        assert "install" in result.fix_hint.lower()


class TestCheckDockerDaemon:
    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_daemon_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        result = check_docker_daemon()
        assert result.status == CheckStatus.OK
        assert result.name == "docker-daemon"

    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_daemon_not_running_cannot_connect(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Cannot connect to the Docker daemon",
        )
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR
        assert result.fix_hint is not None

    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_daemon_not_running_is_daemon_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Is the docker daemon running?",
        )
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR

    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_daemon_generic_error(self, mock_run: MagicMock) -> None:
        """Non-zero return code without 'Cannot connect' gives WARNING."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Some other error message",
        )
        result = check_docker_daemon()
        assert result.status == CheckStatus.WARNING

    @patch(
        "nexus.cli.commands.doctor.subprocess.run",
        side_effect=FileNotFoundError,
    )
    def test_docker_cli_not_found(self, _mock: MagicMock) -> None:
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR
        assert "not found" in result.message.lower()

    @patch(
        "nexus.cli.commands.doctor.subprocess.run",
        side_effect=subprocess.TimeoutExpired("docker", 10),
    )
    def test_daemon_timeout(self, _mock: MagicMock) -> None:
        result = check_docker_daemon()
        assert result.status == CheckStatus.ERROR
        assert "timed out" in result.message.lower()


class TestCheckServerReachable:
    @patch("httpx.Client")
    def test_server_returns_200(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=200)
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = check_server_reachable()
        assert result.status == CheckStatus.OK
        assert result.name == "server-http"

    @patch("httpx.Client")
    def test_server_returns_non_200(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=503)
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = check_server_reachable()
        assert result.status == CheckStatus.WARNING
        assert "503" in result.message

    @patch("httpx.Client", side_effect=Exception("Connection refused"))
    def test_server_unreachable(self, _mock: MagicMock) -> None:
        result = check_server_reachable()
        assert result.status == CheckStatus.WARNING
        assert result.fix_hint is not None

    @patch("httpx.Client")
    def test_server_uses_nexus_url_env(
        self, mock_client_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_URL", "http://custom:9999")
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=200)
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = check_server_reachable()
        assert result.status == CheckStatus.OK
        assert "custom:9999" in result.message


class TestCheckGrpcPort:
    def test_valid_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_GRPC_PORT", "2126")
        result = check_grpc_port()
        assert result.status == CheckStatus.OK
        assert "2126" in result.message

    def test_port_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_GRPC_PORT", "0")
        result = check_grpc_port()
        assert result.status == CheckStatus.WARNING

    def test_port_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_GRPC_PORT", raising=False)
        result = check_grpc_port()
        assert result.status == CheckStatus.WARNING

    def test_port_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_GRPC_PORT", "")
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
        assert "10.0" in result.message

    @patch("nexus.cli.commands.doctor.shutil.disk_usage")
    def test_low_space(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = Mock(free=500_000_000)  # ~0.47 GB
        result = check_disk_space()
        assert result.status == CheckStatus.WARNING
        assert "low disk space" in result.message.lower()
        assert result.fix_hint is not None

    @patch("nexus.cli.commands.doctor.shutil.disk_usage")
    def test_boundary_exactly_1gb(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = Mock(free=1024**3)  # exactly 1 GB
        result = check_disk_space()
        assert result.status == CheckStatus.OK

    @patch(
        "nexus.cli.commands.doctor.shutil.disk_usage",
        side_effect=OSError("Permission denied"),
    )
    def test_os_error(self, _mock: MagicMock) -> None:
        result = check_disk_space()
        assert result.status == CheckStatus.WARNING
        assert "could not check" in result.message.lower()


class TestCheckDataDirWritable:
    def test_writable_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        result = check_data_dir_writable()
        assert result.status == CheckStatus.OK
        assert result.name == "data-dir"

    def test_nonexistent_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv("NEXUS_DATA_DIR", str(missing))
        result = check_data_dir_writable()
        assert result.status == CheckStatus.WARNING
        assert result.fixable is True
        assert result.fix_hint is not None

    def test_not_writable_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        monkeypatch.setenv("NEXUS_DATA_DIR", str(readonly_dir))
        with patch("nexus.cli.commands.doctor.os.access", return_value=False):
            result = check_data_dir_writable()
            assert result.status == CheckStatus.ERROR
            assert result.fixable is False


# ---------------------------------------------------------------------------
# Federation checks
# ---------------------------------------------------------------------------


class TestCheckTlsCerts:
    def test_no_tls_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        result = check_tls_certs()
        assert result.status == CheckStatus.WARNING
        assert result.fixable is True
        assert "no tls/ directory" in result.message.lower()

    def test_complete_tls(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").touch()
        (tls_dir / "node.pem").touch()
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        result = check_tls_certs()
        assert result.status == CheckStatus.OK

    def test_partial_certs_missing_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").touch()
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        result = check_tls_certs()
        assert result.status == CheckStatus.WARNING
        assert "partially" in result.message.lower()

    def test_partial_certs_missing_ca(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "node.pem").touch()
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        result = check_tls_certs()
        assert result.status == CheckStatus.WARNING
        assert result.fixable is True


class TestCheckTlsExpiry:
    def test_no_cert_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        result = check_tls_expiry()
        assert result.status == CheckStatus.WARNING
        assert "no tls certificate" in result.message.lower()

    def test_cert_valid_more_than_30_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").write_text("fake cert")
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))

        mock_cert = MagicMock()
        mock_cert.not_valid_after_utc = datetime.now(UTC) + timedelta(days=365)

        with patch("nexus.security.tls.certgen.load_pem_cert", return_value=mock_cert) as mock_load:
            result = check_tls_expiry()
            mock_load.assert_called_once()
        assert result.status == CheckStatus.OK
        assert "valid for" in result.message.lower()

    def test_cert_expires_within_30_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").write_text("fake cert")
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))

        mock_cert = MagicMock()
        mock_cert.not_valid_after_utc = datetime.now(UTC) + timedelta(days=15)

        with patch("nexus.security.tls.certgen.load_pem_cert", return_value=mock_cert):
            result = check_tls_expiry()
        assert result.status == CheckStatus.WARNING
        assert "expires in" in result.message.lower()
        assert result.fix_hint is not None

    def test_cert_expired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").write_text("fake cert")
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))

        mock_cert = MagicMock()
        mock_cert.not_valid_after_utc = datetime.now(UTC) - timedelta(days=10)

        with patch("nexus.security.tls.certgen.load_pem_cert", return_value=mock_cert):
            result = check_tls_expiry()
        assert result.status == CheckStatus.ERROR
        assert "expired" in result.message.lower()

    def test_cert_parse_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").write_text("corrupt")
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))

        with patch(
            "nexus.security.tls.certgen.load_pem_cert",
            side_effect=ValueError("bad cert"),
        ):
            result = check_tls_expiry()
        assert result.status == CheckStatus.WARNING
        assert "could not check" in result.message.lower()


# ---------------------------------------------------------------------------
# Security checks
# ---------------------------------------------------------------------------


class TestCheckZoneIsolation:
    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "False", "OFF"])
    def test_disabled_variants(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_ENFORCE_ZONE_ISOLATION", value)
        result = check_zone_isolation()
        assert result.status == CheckStatus.WARNING

    def test_enabled_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_ENFORCE_ZONE_ISOLATION", "true")
        result = check_zone_isolation()
        assert result.status == CheckStatus.OK

    def test_default_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_ENFORCE_ZONE_ISOLATION", raising=False)
        result = check_zone_isolation()
        assert result.status == CheckStatus.OK


class TestCheckDatabaseUrl:
    def test_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://localhost/nexus")
        result = check_database_url()
        assert result.status == CheckStatus.OK
        # Ensure URL is NOT leaked in the message
        assert "postgresql://" not in result.message

    def test_url_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        result = check_database_url()
        assert result.status == CheckStatus.WARNING
        assert result.fix_hint is not None


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_current_python_passes(self) -> None:
        # We're running on 3.12+, so this should pass
        result = check_python_version()
        assert result.status == CheckStatus.OK

    @patch("nexus.cli.commands.doctor.sys")
    def test_old_python_fails(self, mock_sys: MagicMock) -> None:
        mock_sys.version_info = (3, 10, 0)
        result = check_python_version()
        assert result.status == CheckStatus.ERROR
        assert "3.12" in result.message or "3.12" in (result.fix_hint or "")


class TestCheckDockerComposeVersion:
    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_compose_v2_available(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="2.27.0\n")
        result = check_docker_compose_version()
        assert result.status == CheckStatus.OK
        assert "2.27.0" in result.message

    @patch("nexus.cli.commands.doctor.subprocess.run")
    def test_compose_returns_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = check_docker_compose_version()
        assert result.status == CheckStatus.ERROR

    @patch(
        "nexus.cli.commands.doctor.subprocess.run",
        side_effect=FileNotFoundError,
    )
    def test_compose_not_found(self, _mock: MagicMock) -> None:
        result = check_docker_compose_version()
        assert result.status == CheckStatus.ERROR

    @patch(
        "nexus.cli.commands.doctor.subprocess.run",
        side_effect=subprocess.TimeoutExpired("docker compose", 5),
    )
    def test_compose_timeout(self, _mock: MagicMock) -> None:
        result = check_docker_compose_version()
        assert result.status == CheckStatus.ERROR


class TestCheckPgvector:
    def test_no_db_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        result = check_pgvector()
        assert result.status == CheckStatus.WARNING

    def test_db_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://localhost/nexus")
        result = check_pgvector()
        assert result.status == CheckStatus.OK


# ---------------------------------------------------------------------------
# _run_all_checks_async
# ---------------------------------------------------------------------------


class TestRunAllChecksAsync:
    def test_runs_all_categories(self) -> None:
        ok_check = lambda: CheckResult("t", CheckStatus.OK, "ok")  # noqa: E731
        with patch(
            "nexus.cli.commands.doctor.CHECKS",
            {"cat1": [ok_check], "cat2": [ok_check]},
        ):
            results = asyncio.run(_run_all_checks_async())
        assert "cat1" in results
        assert "cat2" in results
        assert len(results["cat1"]) == 1
        assert results["cat1"][0].status == CheckStatus.OK

    def test_handles_exception_from_check(self) -> None:
        def bad_check() -> CheckResult:
            msg = "boom"
            raise RuntimeError(msg)

        with patch(
            "nexus.cli.commands.doctor.CHECKS",
            {"failing": [bad_check]},
        ):
            results = asyncio.run(_run_all_checks_async())
        assert results["failing"][0].status == CheckStatus.ERROR
        assert "unexpectedly" in results["failing"][0].message.lower()

    def test_applies_fix_when_requested(self) -> None:
        fixable_result = CheckResult(
            name="data-dir",
            status=CheckStatus.WARNING,
            message="missing",
            fixable=True,
        )
        fixable_check = lambda: fixable_result  # noqa: E731

        fixed_result = CheckResult(name="data-dir", status=CheckStatus.OK, message="fixed")

        with (
            patch("nexus.cli.commands.doctor.CHECKS", {"storage": [fixable_check]}),
            patch("nexus.cli.commands.doctor._try_fix", return_value=fixed_result) as mock_fix,
        ):
            results = asyncio.run(_run_all_checks_async(fix=True))
        mock_fix.assert_called_once_with(fixable_result)
        assert results["storage"][0].status == CheckStatus.OK

    def test_skips_fix_when_not_requested(self) -> None:
        fixable_result = CheckResult(
            name="data-dir",
            status=CheckStatus.WARNING,
            message="missing",
            fixable=True,
        )
        fixable_check = lambda: fixable_result  # noqa: E731

        with (
            patch("nexus.cli.commands.doctor.CHECKS", {"storage": [fixable_check]}),
            patch("nexus.cli.commands.doctor._try_fix") as mock_fix,
        ):
            results = asyncio.run(_run_all_checks_async(fix=False))
        mock_fix.assert_not_called()
        assert results["storage"][0].status == CheckStatus.WARNING


# ---------------------------------------------------------------------------
# _try_fix
# ---------------------------------------------------------------------------


class TestTryFix:
    def test_fix_data_dir_creates_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "create_me"
        result = CheckResult(
            name="data-dir",
            status=CheckStatus.WARNING,
            message="missing",
            fixable=True,
        )
        monkeypatch.setenv("NEXUS_DATA_DIR", str(missing))
        fixed = _try_fix(result)
        assert fixed is not None
        assert fixed.status == CheckStatus.OK
        assert missing.exists()

    def test_fix_data_dir_mkdir_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = CheckResult(
            name="data-dir",
            status=CheckStatus.WARNING,
            message="missing",
            fixable=True,
        )
        monkeypatch.setenv("NEXUS_DATA_DIR", "/root/impossible_dir")
        with patch("pathlib.Path.mkdir", side_effect=OSError("Permission denied")):
            fixed = _try_fix(result)
        assert fixed is not None
        assert fixed.status == CheckStatus.ERROR

    def test_no_fix_for_unfixable(self) -> None:
        result = CheckResult(
            name="something",
            status=CheckStatus.ERROR,
            message="broken",
            fixable=False,
        )
        assert _try_fix(result) is None

    def test_no_fix_for_unknown_name(self) -> None:
        result = CheckResult(
            name="unknown-check",
            status=CheckStatus.WARNING,
            message="unknown",
            fixable=True,
        )
        assert _try_fix(result) is None

    def test_fix_tls_certs_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = CheckResult(
            name="tls-certs",
            status=CheckStatus.WARNING,
            message="no certs",
            fixable=True,
        )
        monkeypatch.setenv("NEXUS_DATA_DIR", "/tmp/tls_test")
        mock_ca_cert = MagicMock()
        mock_ca_key = MagicMock()
        mock_node_cert = MagicMock()
        mock_node_key = MagicMock()
        with (
            patch(
                "nexus.security.tls.certgen.generate_zone_ca",
                return_value=(mock_ca_cert, mock_ca_key),
            ),
            patch(
                "nexus.security.tls.certgen.generate_node_cert",
                return_value=(mock_node_cert, mock_node_key),
            ),
            patch("nexus.security.tls.certgen.save_pem") as mock_save,
        ):
            fixed = _try_fix(result)
        assert fixed is not None
        assert fixed.status == CheckStatus.OK
        assert mock_save.call_count == 4

    def test_fix_tls_certs_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = CheckResult(
            name="tls-certs",
            status=CheckStatus.WARNING,
            message="no certs",
            fixable=True,
        )
        monkeypatch.setenv("NEXUS_DATA_DIR", "/tmp/tls_test")
        with patch(
            "nexus.security.tls.certgen.generate_zone_ca",
            side_effect=RuntimeError("cert gen failed"),
        ):
            fixed = _try_fix(result)
        assert fixed is not None
        assert fixed.status == CheckStatus.ERROR


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------


class TestCheckRegistry:
    def test_all_categories_present(self) -> None:
        expected = {"connectivity", "storage", "federation", "security", "dependencies"}
        assert set(CHECKS.keys()) == expected

    def test_all_checks_are_callable(self) -> None:
        for category, checks in CHECKS.items():
            for check_fn in checks:
                assert callable(check_fn), f"{category}: {check_fn} is not callable"


# ---------------------------------------------------------------------------
# CLI command (doctor)
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_json_output(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.OK, "Docker found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor, ["--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        # render_output wraps data in {"data": ..., "_timing": ...} envelope
        assert "data" in parsed
        assert "connectivity" in parsed["data"]
        assert parsed["data"]["connectivity"][0]["status"] == "ok"

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_table_output(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.OK, "Docker found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor)
        assert result.exit_code == 0

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_exit_code_on_error(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.ERROR, "Not found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor)
        assert result.exit_code == 1

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_fix_flag_passed_through(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.OK, "ok"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor, ["--fix"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(fix=True)


# ---------------------------------------------------------------------------
# Regression tests — group conversion must not change bare doctor behaviour
# ---------------------------------------------------------------------------


class TestDoctorGroupRegressions:
    """Prove that converting doctor to a group doesn't change bare-doctor behaviour."""

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_bare_doctor_runs_checks(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """nexus doctor (no subcommand) still runs _run_all_checks_async."""

        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.OK, "Docker found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor, [])
        assert result.exit_code == 0
        mock_run.assert_called_once()

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_bare_doctor_json(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """nexus doctor --json still emits the JSON envelope."""

        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.OK, "Docker found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor, ["--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "data" in parsed
        assert "connectivity" in parsed["data"]

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_bare_doctor_fix(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """nexus doctor --fix passes fix=True to _run_all_checks_async."""

        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.OK, "Docker found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor, ["--fix"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(fix=True)

    @patch("nexus.cli.commands.doctor._run_all_checks_async")
    def test_bare_doctor_error_exit_code(
        self,
        mock_run: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """nexus doctor exits 1 when any check is ERROR."""

        async def fake_run(fix: bool = False) -> dict:
            return {
                "connectivity": [
                    CheckResult("docker", CheckStatus.ERROR, "Not found"),
                ],
            }

        mock_run.side_effect = fake_run
        result = cli_runner.invoke(doctor, [])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# nexus doctor remote — new preflight subcommand
# ---------------------------------------------------------------------------


class TestDoctorRemote:
    """Tests for `nexus doctor remote --url <URL> --api-key <KEY>`."""

    def _make_http_mock(self, status_code: int = 200) -> MagicMock:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=status_code)
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        return mock_client

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_happy_path_both_ok(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both HTTP and gRPC healthy → exit 0, both results OK."""
        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.return_value = True
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote, ["--url", "http://hub.example.com:2026", "--api-key", "testkey"]
        )
        assert result.exit_code == 0
        # No traceback
        assert "Traceback" not in result.output
        # transport is closed
        mock_transport.close.assert_called_once()

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_happy_path_json(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json mode emits valid JSON with both checks OK."""
        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.return_value = True
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote,
            ["--url", "http://hub.example.com:2026", "--api-key", "testkey", "--json"],
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "data" in parsed
        checks = parsed["data"]
        statuses = {c["name"]: c["status"] for c in checks}
        assert statuses.get("remote-http") == "ok"
        assert statuses.get("remote-grpc") == "ok"

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_http_ok_grpc_unreachable(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTP 200 but gRPC unreachable → exit 1, ERROR result, actionable hint."""
        from nexus.contracts.exceptions import RemoteConnectionError

        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.side_effect = RemoteConnectionError(
            "gRPC server unavailable", details={}, method="Ping"
        )
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote, ["--url", "http://hub.example.com:2026", "--api-key", "testkey"]
        )
        assert result.exit_code == 1
        # No raw traceback
        assert "Traceback" not in result.output
        # Actionable hint: should mention NEXUS_GRPC_PORT
        assert "NEXUS_GRPC_PORT" in result.output
        # Transport is closed despite failure
        mock_transport.close.assert_called_once()

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_grpc_unreachable_json(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json mode with gRPC failure emits ERROR check result and exits 1."""
        from nexus.contracts.exceptions import RemoteConnectionError

        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.side_effect = RemoteConnectionError(
            "gRPC server unavailable", details={}, method="Ping"
        )
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote,
            ["--url", "http://hub.example.com:2026", "--api-key", "testkey", "--json"],
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        checks = parsed["data"]
        grpc_check = next(c for c in checks if c["name"] == "remote-grpc")
        assert grpc_check["status"] == "error"
        assert grpc_check["fix_hint"] is not None
        assert "NEXUS_GRPC_PORT" in grpc_check["fix_hint"]

    @patch("httpx.Client")
    def test_insecure_non_loopback_surfaces_as_error(
        self,
        mock_http_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RPCTransport ValueError (insecure non-loopback) → ERROR (non-zero
        exit), not a soft WARNING: the real remote SDK connection would be
        refused the same way, so a preflight must fail. Still actionable,
        no traceback.
        """
        # Remove the escape hatch so RPCTransport refuses the insecure channel
        monkeypatch.delenv("NEXUS_GRPC_ALLOW_INSECURE", raising=False)
        mock_http_cls.return_value = self._make_http_mock(200)

        with patch(
            "nexus.remote.rpc_transport.RPCTransport",
            side_effect=ValueError(
                "Insecure gRPC channel refused for non-loopback address 'hub.example.com:2028'."
            ),
        ):
            result = cli_runner.invoke(
                doctor_remote, ["--url", "http://hub.example.com:2026", "--api-key", "testkey"]
            )

        assert result.exit_code != 0  # unusable remote path ⇒ preflight fails
        assert "Traceback" not in result.output
        assert "NEXUS_GRPC_ALLOW_INSECURE" in result.output or "TLS" in result.output

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_http_non200_is_error_nonzero(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-200 HTTP health response means the remote path is not
        usable → preflight is an ERROR with non-zero exit (not a soft
        WARNING/exit 0), even if gRPC happens to be reachable."""
        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(503)
        mock_transport = MagicMock()
        mock_transport.health_check.return_value = True  # gRPC fine
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote,
            ["--url", "http://hub.example.com:2026", "--api-key", "testkey", "--json"],
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        http_check = next(c for c in parsed["data"] if c["name"] == "remote-http")
        assert http_check["status"] == "error"
        assert "503" in http_check["message"]
        assert "Traceback" not in result.output

    @patch("httpx.Client")
    def test_invalid_grpc_port_is_actionable_error(
        self,
        mock_http_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An invalid NEXUS_GRPC_PORT must surface as an actionable ERROR
        CheckResult (the SDK fails the same way) — not a silent wrong-port
        dial and not a raw traceback."""
        monkeypatch.setenv("NEXUS_GRPC_PORT", "notaport")
        mock_http_cls.return_value = self._make_http_mock(200)

        result = cli_runner.invoke(
            doctor_remote,
            ["--url", "http://hub.example.com:2026", "--api-key", "k", "--json"],
        )
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        grpc_check = next(
            c for c in json.loads(result.output)["data"] if c["name"] == "remote-grpc"
        )
        assert grpc_check["status"] == "error"
        assert "port" in grpc_check["message"].lower()

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_grpc_auth_failure_is_diagnosed_distinctly(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An auth failure on the gRPC Ping path (UNAUTHENTICATED /
        PERMISSION_DENIED) must be reported as an AUTH problem with a
        key-specific fix hint — NOT misdiagnosed as an unreachable port
        / firewall issue (which would send the operator the wrong way)."""
        from nexus.contracts.exceptions import RemoteConnectionError

        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.side_effect = RemoteConnectionError(
            "gRPC health check failed: <_InactiveRpcError "
            "StatusCode.UNAUTHENTICATED: missing bearer token>",
            details={},
            method="Ping",
        )
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote,
            ["--url", "http://hub.example.com:2026", "--api-key", "", "--json"],
        )
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        grpc_check = next(
            c for c in json.loads(result.output)["data"] if c["name"] == "remote-grpc"
        )
        assert grpc_check["status"] == "error"
        # auth-specific, NOT a port/firewall misdiagnosis
        assert "auth" in grpc_check["message"].lower()
        assert "API key" in grpc_check["fix_hint"]
        assert "NEXUS_GRPC_PORT" not in grpc_check["fix_hint"]
        mock_transport.close.assert_called_once()

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_grpc_port_from_env(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """NEXUS_GRPC_PORT env var is used to build the gRPC address."""
        monkeypatch.setenv("NEXUS_GRPC_PORT", "9999")
        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.return_value = True
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(
            doctor_remote, ["--url", "http://hub.example.com:2026", "--api-key", "testkey"]
        )
        assert result.exit_code == 0
        # RPCTransport should have been constructed with the custom port
        call_kwargs = mock_rpc_cls.call_args
        assert "hub.example.com:9999" in str(call_kwargs)

    @patch("nexus.remote.rpc_transport.RPCTransport")
    @patch("httpx.Client")
    def test_url_from_env(
        self,
        mock_http_cls: MagicMock,
        mock_rpc_cls: MagicMock,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--url can be supplied via NEXUS_URL env var."""
        monkeypatch.setenv("NEXUS_URL", "http://hub.example.com:2026")
        monkeypatch.setenv("NEXUS_API_KEY", "envkey")
        monkeypatch.setenv("NEXUS_GRPC_ALLOW_INSECURE", "true")
        mock_http_cls.return_value = self._make_http_mock(200)
        mock_transport = MagicMock()
        mock_transport.health_check.return_value = True
        mock_rpc_cls.return_value = mock_transport

        result = cli_runner.invoke(doctor_remote, [])
        assert result.exit_code == 0

    def test_no_url_raises_usage_error(
        self,
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor remote with no --url and no NEXUS_URL → non-zero exit, actionable message."""
        monkeypatch.delenv("NEXUS_URL", raising=False)

        result = cli_runner.invoke(doctor_remote, [], env={"NEXUS_URL": ""})

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        # The error message must tell the user what to provide
        assert "NEXUS_URL" in result.output or "url" in result.output.lower()
