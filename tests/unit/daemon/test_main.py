"""Unit tests for the nexusd daemon entry point.

Tests cover:
- ``_redact_url()`` — password redaction in database URLs
- ``_JsonLogFormatter`` — structured JSON log output
- ``_manage_pid_file()`` — PID file lifecycle (create / stale / running)
- ``main()`` — Click CLI command via CliRunner
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.daemon.main import (
    _is_nexusd_process,
    _JsonLogFormatter,
    _manage_pid_file,
    _print_lifecycle_summary,
    _redact_url,
    main,
)

# ---------------------------------------------------------------------------
# _redact_url
# ---------------------------------------------------------------------------


class TestRedactUrl:
    """Tests for ``_redact_url``."""

    def test_redact_url_with_password(self) -> None:
        url = "postgresql://user:s3cret@db.example.com:5432/nexus"
        result = _redact_url(url)

        assert "s3cret" not in result
        assert "****" in result
        assert "user" in result
        assert "db.example.com" in result

    def test_redact_url_without_password(self) -> None:
        url = "postgresql://db.example.com:5432/nexus"
        result = _redact_url(url)

        assert result == url

    def test_redact_url_invalid(self) -> None:
        url = "not-a-url"
        result = _redact_url(url)

        assert result == url


# ---------------------------------------------------------------------------
# _JsonLogFormatter
# ---------------------------------------------------------------------------


class TestJsonLogFormatter:
    """Tests for ``_JsonLogFormatter``."""

    def test_json_log_formatter(self) -> None:
        formatter = _JsonLogFormatter()
        record = logging.LogRecord(
            name="nexusd",
            level=logging.INFO,
            pathname="main.py",
            lineno=1,
            msg="daemon started",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "info"
        assert parsed["logger"] == "nexusd"
        assert parsed["msg"] == "daemon started"
        assert "ts" in parsed
        assert "error" not in parsed

    def test_json_log_formatter_with_exception(self) -> None:
        formatter = _JsonLogFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="nexusd",
            level=logging.ERROR,
            pathname="main.py",
            lineno=42,
            msg="something failed",
            args=(),
            exc_info=exc_info,
        )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "error"
        assert parsed["error"] == "boom"
        assert parsed["msg"] == "something failed"


# ---------------------------------------------------------------------------
# _is_nexusd_process
# ---------------------------------------------------------------------------


class TestIsNexusdProcess:
    """Tests for ``_is_nexusd_process``."""

    def test_nonexistent_pid(self) -> None:
        # PID above kernel max — guaranteed not to exist
        assert _is_nexusd_process(4194305) is False

    def test_proc_fs_unavailable_returns_true(self) -> None:
        """Without /proc (macOS, BSD), fall back conservatively to True."""
        pid = os.getpid()
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError):
            assert _is_nexusd_process(pid) is True

    def test_linux_cmdline_without_nexusd_returns_false(self) -> None:
        """On Linux, a live PID whose cmdline doesn't mention nexusd is stale."""
        pid = os.getpid()
        # Simulate /proc/<pid>/cmdline for a pytest process
        fake_cmdline = b"/usr/bin/python3\x00-m\x00pytest\x00tests/\x00"
        with patch.object(Path, "read_bytes", return_value=fake_cmdline):
            assert _is_nexusd_process(pid) is False

    def test_pid_reuse_different_process(self, tmp_path: Path) -> None:
        # Simulate /proc/<pid>/cmdline pointing to a non-nexusd process
        # by patching Path so that /proc/<pid>/cmdline returns "bash"
        pid = os.getpid()
        fake_cmdline = b"bash\x00--login\x00"

        with patch.object(Path, "read_bytes", return_value=fake_cmdline):
            # Also need /proc/{pid}/cmdline to "exist" — don't raise FileNotFoundError
            result = _is_nexusd_process(pid)

        assert result is False

    def test_cmdline_contains_nexusd(self) -> None:
        pid = os.getpid()
        fake_cmdline = b"python3\x00-m\x00nexus.daemon\x00--port\x002026\x00"

        with patch.object(Path, "read_bytes", return_value=fake_cmdline):
            result = _is_nexusd_process(pid)

        assert result is True

    def test_cmdline_contains_nexusd_script(self) -> None:
        pid = os.getpid()
        fake_cmdline = b"/usr/local/bin/nexusd\x00--host\x000.0.0.0\x00"

        with patch.object(Path, "read_bytes", return_value=fake_cmdline):
            result = _is_nexusd_process(pid)

        assert result is True


# ---------------------------------------------------------------------------
# _manage_pid_file
# ---------------------------------------------------------------------------


class TestManagePidFile:
    """Tests for ``_manage_pid_file`` lifecycle."""

    def test_manage_pid_file_creates_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        pid_path = _manage_pid_file()

        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())
        # Cleanup
        pid_path.unlink(missing_ok=True)

    def test_manage_pid_file_removes_stale(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        nexus_dir = tmp_path / ".nexus"
        nexus_dir.mkdir(parents=True, exist_ok=True)
        pid_file = nexus_dir / "nexusd.pid"
        # Write a PID that definitely does not exist (kernel max + 1 is safe)
        pid_file.write_text("4194305")

        pid_path = _manage_pid_file()

        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())
        # Cleanup
        pid_path.unlink(missing_ok=True)

    def test_manage_pid_file_exits_if_running(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        nexus_dir = tmp_path / ".nexus"
        nexus_dir.mkdir(parents=True, exist_ok=True)
        pid_file = nexus_dir / "nexusd.pid"
        # Use the current process PID — guaranteed to be running.
        # Patch _is_nexusd_process so the test works identically on Linux
        # (where /proc/<pid>/cmdline would show "pytest", not "nexusd").
        pid_file.write_text(str(os.getpid()))

        with (
            patch("nexus.daemon.main._is_nexusd_process", return_value=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            _manage_pid_file()

        assert exc_info.value.code == 78  # CONFIG_ERROR
        # Cleanup
        pid_file.unlink(missing_ok=True)

    def test_manage_pid_file_clears_reused_pid(self, tmp_path: Path, monkeypatch) -> None:
        """PID file points to a live process that is NOT nexusd (PID reuse)."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        nexus_dir = tmp_path / ".nexus"
        nexus_dir.mkdir(parents=True, exist_ok=True)
        pid_file = nexus_dir / "nexusd.pid"
        # Current PID is alive but _is_nexusd_process will say False
        pid_file.write_text(str(os.getpid()))

        with patch("nexus.daemon.main._is_nexusd_process", return_value=False):
            pid_path = _manage_pid_file()

        # Should have replaced the stale file with our own PID
        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())
        pid_path.unlink(missing_ok=True)

    def test_manage_pid_file_handles_corrupt_content(self, tmp_path: Path, monkeypatch) -> None:
        """PID file contains non-numeric garbage."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        nexus_dir = tmp_path / ".nexus"
        nexus_dir.mkdir(parents=True, exist_ok=True)
        pid_file = nexus_dir / "nexusd.pid"
        pid_file.write_text("not-a-number\n")

        pid_path = _manage_pid_file()

        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())
        pid_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# main() — Click CLI via CliRunner
# ---------------------------------------------------------------------------


class TestMainCli:
    """Click CliRunner tests for the ``main`` command."""

    def test_main_remote_profile_rejected(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["--profile", "remote"])

        assert result.exit_code == 78
        assert (
            "remote"
            in (result.output + (result.stderr if hasattr(result, "stderr") else "")).lower()
        )

    def test_main_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])

        assert result.exit_code == 0
        assert "nexusd" in result.output

    def test_main_happy_path(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import sys
        import types

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        mock_nx = MagicMock()
        mock_connect = MagicMock(return_value=mock_nx)

        mock_app = MagicMock()
        mock_create_app = MagicMock(return_value=mock_app)
        mock_run_server = MagicMock()

        # Inject a fake fastapi_server module so the lazy import inside main()
        # resolves without requiring the real fastapi dependency.
        fake_mod = types.ModuleType("nexus.server.fastapi_server")
        fake_mod.create_app = mock_create_app
        fake_mod.run_server = mock_run_server
        monkeypatch.setitem(sys.modules, "nexus.server.fastapi_server", fake_mod)

        with patch("nexus.connect", mock_connect):
            runner = CliRunner()
            result = runner.invoke(main, ["--host", "127.0.0.1", "--port", "9999"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        mock_connect.assert_called_once()
        mock_create_app.assert_called_once()
        mock_run_server.assert_called_once()

        # Verify host/port were forwarded to run_server
        call_kwargs = mock_run_server.call_args
        assert call_kwargs.kwargs["host"] == "127.0.0.1"
        assert call_kwargs.kwargs["port"] == 9999

    def test_main_lifecycle_summary_shown(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """Lifecycle summary line appears for non-innovation profiles (Issue #1578)."""
        import sys
        import types

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        # Build mock with lifecycle coordinator
        mock_q = MagicMock(is_persistent=True, is_hot_swappable=False)
        mock_q2 = MagicMock(is_persistent=False, is_hot_swappable=True)
        mock_coordinator = MagicMock()
        mock_coordinator.classify_all.return_value = {"svc_a": mock_q, "svc_b": mock_q2}

        mock_nx = MagicMock()
        mock_nx._lifecycle_coordinator = mock_coordinator
        mock_connect = MagicMock(return_value=mock_nx)

        mock_app = MagicMock()
        mock_create_app = MagicMock(return_value=mock_app)
        mock_run_server = MagicMock()

        fake_mod = types.ModuleType("nexus.server.fastapi_server")
        fake_mod.create_app = mock_create_app
        fake_mod.run_server = mock_run_server
        monkeypatch.setitem(sys.modules, "nexus.server.fastapi_server", fake_mod)

        with patch("nexus.connect", mock_connect):
            runner = CliRunner()
            result = runner.invoke(main, ["--profile", "full"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Lifecycle:" in result.output
        assert "2 services" in result.output
        assert "distro=persistent" in result.output
        # Not innovation — no quadrant detail
        assert "[validation]" not in result.output


# ---------------------------------------------------------------------------
# _print_lifecycle_summary (Issue #1578)
# ---------------------------------------------------------------------------


class TestLifecycleReport:
    """Unit tests for lifecycle summary functions."""

    def test_summary_with_coordinator(self, capsys) -> None:
        mock_q1 = MagicMock(is_persistent=True, is_hot_swappable=True)
        mock_q2 = MagicMock(is_persistent=False, is_hot_swappable=True)
        mock_q3 = MagicMock(is_persistent=True, is_hot_swappable=False)
        coordinator = MagicMock()
        coordinator.classify_all.return_value = {
            "a": mock_q1,
            "b": mock_q2,
            "c": mock_q3,
        }
        nx = MagicMock()
        nx._lifecycle_coordinator = coordinator

        _print_lifecycle_summary(nx)
        out = capsys.readouterr().out

        assert "Lifecycle:" in out
        assert "3 services" in out
        assert "2 hot-swappable" in out
        assert "2 persistent" in out
        assert "distro=persistent" in out

    def test_summary_on_demand(self, capsys) -> None:
        mock_q = MagicMock(is_persistent=False, is_hot_swappable=False)
        coordinator = MagicMock()
        coordinator.classify_all.return_value = {"svc": mock_q}
        nx = MagicMock()
        nx._lifecycle_coordinator = coordinator

        _print_lifecycle_summary(nx)
        out = capsys.readouterr().out

        assert "distro=on-demand" in out

    def test_summary_no_coordinator(self, capsys) -> None:
        nx = MagicMock(spec=[])  # no attributes at all
        _print_lifecycle_summary(nx)
        out = capsys.readouterr().out
        assert out == ""

    def test_summary_exception_swallowed(self, capsys) -> None:
        coordinator = MagicMock()
        coordinator.classify_all.side_effect = RuntimeError("boom")
        nx = MagicMock()
        nx._lifecycle_coordinator = coordinator

        _print_lifecycle_summary(nx)  # should not raise
        out = capsys.readouterr().out
        assert out == ""
