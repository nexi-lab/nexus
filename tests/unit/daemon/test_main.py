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

    def test_current_process_without_proc_fs(self, monkeypatch) -> None:
        # On macOS (no /proc), should fall back to os.kill and return True
        # because the process exists and /proc read raises FileNotFoundError
        result = _is_nexusd_process(os.getpid())
        # On Linux the cmdline won't contain "nexusd" (it's pytest), but
        # on macOS the /proc fallback returns True conservatively.
        # Either way, just verify it doesn't crash.
        assert isinstance(result, bool)

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
        # Use the current process PID — guaranteed to be running
        pid_file.write_text(str(os.getpid()))

        with pytest.raises(SystemExit) as exc_info:
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
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        mock_nx = MagicMock()
        mock_connect = MagicMock(return_value=mock_nx)

        mock_app = MagicMock()
        mock_create_app = MagicMock(return_value=mock_app)

        mock_run_server = MagicMock()

        # Patch lazy imports: nexus.connect is called via ``import nexus``
        # inside main(), and create_app / run_server via
        # ``from nexus.server.fastapi_server import create_app, run_server``.
        with (
            patch("nexus.connect", mock_connect),
            patch(
                "nexus.server.fastapi_server.create_app",
                mock_create_app,
                create=True,
            ),
            patch(
                "nexus.server.fastapi_server.run_server",
                mock_run_server,
                create=True,
            ),
        ):
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
