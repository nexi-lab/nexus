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
    _should_default_admin_bypass,
    _will_use_static_admin_fallback,
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

        _scoped, pid_path = _manage_pid_file(None)

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

        _scoped, pid_path = _manage_pid_file(None)

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
            _manage_pid_file(None)

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
            _scoped, pid_path = _manage_pid_file(None)

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

        _scoped, pid_path = _manage_pid_file(None)

        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())
        pid_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Readiness file: atomic write + ownership-checked unlink (Issue #4126 r4)
# ---------------------------------------------------------------------------


class TestReadinessFile:
    """``_write_readiness_atomic`` / ``_remove_readiness_if_owned`` / scoping.

    Locks Finding A: two daemons under the SAME HOME with distinct data
    dirs/ports must both stay discoverable, and a daemon exiting must NEVER
    remove a readiness file that no longer carries its own token.
    """

    def test_atomic_write_first_line_is_host_port(self, tmp_path: Path) -> None:
        """Back-compat: first line is ``host:port`` for pre-r4 readers."""
        from nexus.daemon.main import _write_readiness_atomic

        p = tmp_path / "nexusd.ready"
        _write_readiness_atomic(p, "127.0.0.1", 2026)

        lines = p.read_text().splitlines()
        assert lines[0] == "127.0.0.1:2026"
        assert any(ln.startswith("pid=") for ln in lines[1:])

    def test_owned_unlink_removes_own_file(self, tmp_path: Path) -> None:
        from nexus.daemon.main import (
            _remove_readiness_if_owned,
            _write_readiness_atomic,
        )

        p = tmp_path / "nexusd.ready"
        _write_readiness_atomic(p, "127.0.0.1", 2026)
        _remove_readiness_if_owned(p, "127.0.0.1", 2026)
        assert not p.exists()

    def test_owned_unlink_spares_foreign_token(self, tmp_path: Path) -> None:
        """Finding A: a daemon exiting must NOT remove a readiness file whose
        token (pid) differs from its own — that's a still-running sibling."""
        from nexus.daemon.main import _remove_readiness_if_owned

        p = tmp_path / "nexusd.ready"
        # A sibling daemon (different pid) owns this file.
        foreign_pid = os.getpid() + 1
        p.write_text(f"127.0.0.1:2026\npid={foreign_pid}\n")

        _remove_readiness_if_owned(p, "127.0.0.1", 2026)

        assert p.exists(), "must not delete a sibling daemon's readiness file"
        assert f"pid={foreign_pid}" in p.read_text()

    def test_two_daemons_same_home_distinct_scoped_files(self, tmp_path: Path, monkeypatch) -> None:
        """Finding A: two sandboxes under one HOME with distinct data dirs get
        distinct scoped readiness records; one exiting leaves the other's
        scoped record intact AND does not clobber the legacy global file the
        other (last) wrote."""
        from nexus.daemon.main import (
            _remove_readiness_if_owned,
            _scoped_readiness_path,
            _write_readiness_atomic,
        )

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        dd_a = tmp_path / "data_a"
        dd_b = tmp_path / "data_b"
        legacy = tmp_path / ".nexus" / "nexusd.ready"

        scoped_a = _scoped_readiness_path(str(dd_a))
        scoped_b = _scoped_readiness_path(str(dd_b))
        assert scoped_a is not None and scoped_b is not None
        assert scoped_a != scoped_b

        # Daemon A boots, then daemon B boots (B is last writer of legacy).
        _write_readiness_atomic(scoped_a, "127.0.0.1", 2026)
        _write_readiness_atomic(legacy, "127.0.0.1", 2026)
        # Simulate B owning the legacy file (different token).
        legacy.write_text("127.0.0.1:2126\npid=999999\n")
        _write_readiness_atomic(scoped_b, "127.0.0.1", 2126)

        # Daemon A exits: ownership-checked unlink on legacy + scoped_a.
        _remove_readiness_if_owned(legacy, "127.0.0.1", 2026)
        _remove_readiness_if_owned(scoped_a, "127.0.0.1", 2026)

        # B's scoped record + the legacy file B wrote must survive.
        assert scoped_b.exists(), "sibling B scoped readiness must survive A exit"
        assert legacy.exists(), "B's legacy readiness must not be clobbered by A"
        assert "pid=999999" in legacy.read_text()
        assert not scoped_a.exists()

    def test_scoped_path_none_without_data_dir(self) -> None:
        """No data dir → only the legacy global path is used (back-compat)."""
        from nexus.daemon.main import _scoped_readiness_path

        assert _scoped_readiness_path(None) is None
        assert _scoped_readiness_path("") is None


# ---------------------------------------------------------------------------
# Scoped PID file: concurrent same-HOME sandboxes (Issue #4126 r5, Finding A)
# ---------------------------------------------------------------------------


class TestScopedPidFile:
    """``_scoped_pid_path`` / ``_manage_pid_file`` per-instance scoping.

    Locks Finding A: two sandbox daemons under the SAME HOME with distinct
    data dirs must BOTH pass the PID gate (the global ``~/.nexus/nexusd.pid``
    must not block a different-data-dir start), while a genuine double-start
    on the SAME data dir is still rejected, and the single-daemon default
    (no data dir) keeps the legacy global double-start prevention.
    """

    def test_scoped_pid_path_mirrors_readiness_scoping(self, tmp_path: Path) -> None:
        """Scoped PID path is ``<data_dir>/.nexusd.pid`` and distinct per
        data dir; ``None`` when no data dir (legacy-only / back-compat)."""
        from nexus.daemon.main import _scoped_pid_path

        assert _scoped_pid_path(None) is None
        assert _scoped_pid_path("") is None

        dd_a = tmp_path / "data_a"
        dd_b = tmp_path / "data_b"
        pa = _scoped_pid_path(str(dd_a))
        pb = _scoped_pid_path(str(dd_b))
        assert pa is not None and pb is not None
        assert pa != pb
        assert pa == dd_a.resolve() / ".nexusd.pid"
        assert pb == dd_b.resolve() / ".nexusd.pid"

    def test_two_daemons_same_home_distinct_data_dirs_both_pass_gate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Finding A (core): a second daemon under the same HOME with a
        DIFFERENT data dir must pass the PID gate even though the first
        daemon left a live global ``~/.nexus/nexusd.pid``.

        Pre-fix this FAILS: ``_manage_pid_file`` gated only on the single
        global path and ``sys.exit``ed the second daemon."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        dd_a = tmp_path / "data_a"
        dd_b = tmp_path / "data_b"
        dd_a.mkdir(parents=True, exist_ok=True)
        dd_b.mkdir(parents=True, exist_ok=True)

        # Daemon A is "running": it owns the legacy global pid AND its scoped
        # pid (simulate a live nexusd by patching the liveness probe True).
        legacy = tmp_path / ".nexus" / "nexusd.pid"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(str(os.getpid()))
        (dd_a / ".nexusd.pid").write_text(str(os.getpid()))

        with patch("nexus.daemon.main._is_nexusd_process", return_value=True):
            # Daemon B (different data dir) must NOT be rejected by the
            # foreign global pid — its scoped instance is free.
            scoped_pid_b, legacy_pid = _manage_pid_file(str(dd_b))

        assert scoped_pid_b is not None
        assert scoped_pid_b == (dd_b / ".nexusd.pid")
        assert scoped_pid_b.read_text().strip() == str(os.getpid())
        # Legacy global must NOT have been clobbered (A still owns it).
        assert legacy.read_text().strip() == str(os.getpid())
        for p in (scoped_pid_b, legacy_pid):
            if p is not None and p != legacy:
                p.unlink(missing_ok=True)

    def test_same_data_dir_live_pid_still_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """Regression: a genuine double-start on the SAME data dir (a live
        scoped pid) is still rejected — double-start prevention preserved."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        dd = tmp_path / "data"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / ".nexusd.pid").write_text(str(os.getpid()))

        with (
            patch("nexus.daemon.main._is_nexusd_process", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            _manage_pid_file(str(dd))
        assert exc.value.code == 78  # CONFIG_ERROR

    def test_stale_global_pid_does_not_block_scoped_start(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A stale/foreign global pid must NOT block a different-data-dir
        start: the scoped instance is what gates."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        dd = tmp_path / "data"
        dd.mkdir(parents=True, exist_ok=True)
        legacy = tmp_path / ".nexus" / "nexusd.pid"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        # A *live* foreign nexusd owns the global pid (different instance).
        legacy.write_text(str(os.getpid()))

        with patch("nexus.daemon.main._is_nexusd_process", return_value=True):
            scoped_pid, _legacy = _manage_pid_file(str(dd))

        assert scoped_pid is not None
        assert scoped_pid.read_text().strip() == str(os.getpid())
        scoped_pid.unlink(missing_ok=True)

    def test_single_daemon_default_legacy_double_start_preserved(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Back-compat: with NO data dir (single-daemon default) a live
        legacy global pid STILL rejects a second daemon exactly as before."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        legacy = tmp_path / ".nexus" / "nexusd.pid"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(str(os.getpid()))

        with (
            patch("nexus.daemon.main._is_nexusd_process", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            _manage_pid_file(None)
        assert exc.value.code == 78  # CONFIG_ERROR

    def test_single_daemon_default_returns_legacy_path(self, tmp_path: Path, monkeypatch) -> None:
        """No data dir → legacy global path written, scoped path is None
        (single-daemon back-compat: identical observable artifact)."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        scoped_pid, legacy_pid = _manage_pid_file(None)

        assert scoped_pid is None
        assert legacy_pid == tmp_path / ".nexus" / "nexusd.pid"
        assert legacy_pid.read_text().strip() == str(os.getpid())
        legacy_pid.unlink(missing_ok=True)

    def test_remove_pid_files_is_ownership_aware(self, tmp_path: Path) -> None:
        """``_remove_pid_file`` only unlinks a pid file recording THIS
        process (or a stale/dead pid) — never a live sibling's."""
        from nexus.daemon.main import _remove_pid_file

        own = tmp_path / "own.pid"
        own.write_text(str(os.getpid()))
        sibling = tmp_path / "sibling.pid"
        sibling.write_text(str(os.getpid()))

        # Our own pid file: removed.
        _remove_pid_file(own)
        assert not own.exists()

        # A sibling's file recording a DIFFERENT live nexusd pid: spared.
        sibling.write_text("424242")
        with patch("nexus.daemon.main._is_nexusd_process", return_value=True):
            _remove_pid_file(sibling)
        assert sibling.exists(), "must not remove a live sibling's pid file"
        sibling.unlink(missing_ok=True)


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


# ---------------------------------------------------------------------------
# Issue #4126 review r6, Finding B: config-file data_dir must be seen
# BEFORE PID/readiness scoping
# ---------------------------------------------------------------------------


class TestResolveEffectiveDataDir:
    """``_resolve_effective_data_dir`` replicates ``nexus.config``
    precedence so PID/readiness scoping keys off the data dir the daemon
    will ACTUALLY use — not just the Click ``--data-dir`` option computed
    before ``--config`` is loaded."""

    def test_no_config_explicit_data_dir_wins(self) -> None:
        from nexus.daemon.main import _resolve_effective_data_dir

        assert _resolve_effective_data_dir("/explicit/dd", None, "sandbox") == "/explicit/dd"
        assert _resolve_effective_data_dir("/explicit/dd", None, "full") == "/explicit/dd"

    def test_no_config_no_data_dir_sandbox_default(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.daemon.main import _resolve_effective_data_dir

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert _resolve_effective_data_dir(None, None, "sandbox") == str(
            tmp_path / ".nexus" / "sandbox"
        )

    def test_no_config_no_data_dir_nonsandbox_keeps_legacy_none(self) -> None:
        """Back-compat: no --data-dir, no --config, non-sandbox ⇒ None ⇒
        legacy shared global PID/readiness paths (unchanged)."""
        from nexus.daemon.main import _resolve_effective_data_dir

        assert _resolve_effective_data_dir(None, None, "full") is None

    def test_config_file_data_dir_overrides_env(self, tmp_path: Path, monkeypatch) -> None:
        """``--config`` branch: ``load_config`` does
        ``merged=_build_env_overrides(); merged.update(config_dict)`` so a
        config FILE ``data_dir`` overrides ``$NEXUS_DATA_DIR``."""
        from nexus.daemon.main import _resolve_effective_data_dir

        cfg = tmp_path / "sandbox.yaml"
        cfg.write_text("profile: sandbox\ndata_dir: /from/config/file\n")
        monkeypatch.setenv("NEXUS_DATA_DIR", "/from/env")
        assert _resolve_effective_data_dir(None, str(cfg), "sandbox") == "/from/config/file"

    def test_config_no_data_dir_falls_back_to_env(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.daemon.main import _resolve_effective_data_dir

        cfg = tmp_path / "sandbox.yaml"
        cfg.write_text("profile: sandbox\n")  # no data_dir key
        monkeypatch.setenv("NEXUS_DATA_DIR", "/from/env")
        assert _resolve_effective_data_dir(None, str(cfg), "sandbox") == "/from/env"

    def test_config_no_data_dir_no_env_sandbox_default(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.daemon.main import _resolve_effective_data_dir

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
        cfg = tmp_path / "sandbox.yaml"
        cfg.write_text("profile: sandbox\n")
        assert _resolve_effective_data_dir(None, str(cfg), "sandbox") == str(
            tmp_path / ".nexus" / "sandbox"
        )

    def test_two_distinct_config_files_distinct_scoped_paths(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Finding B core: two sandbox config FILES under one HOME with
        DIFFERENT ``data_dir`` (NO ``--data-dir`` flag) resolve to distinct
        effective data dirs ⇒ distinct scoped PID + readiness paths.

        Pre-fix ``_manage_pid_file(data_dir)``/``_scoped_readiness_path
        (data_dir)`` were fed the Click ``--data-dir`` (``None`` here) ⇒
        BOTH collapsed to the shared global paths, re-blocking concurrent
        same-HOME sandboxes."""
        from nexus.daemon.main import (
            _resolve_effective_data_dir,
            _scoped_pid_path,
            _scoped_readiness_path,
        )

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        cfg_a = tmp_path / "a.yaml"
        cfg_b = tmp_path / "b.yaml"
        cfg_a.write_text(f"profile: sandbox\ndata_dir: {tmp_path / 'dd_a'}\n")
        cfg_b.write_text(f"profile: sandbox\ndata_dir: {tmp_path / 'dd_b'}\n")

        eff_a = _resolve_effective_data_dir(None, str(cfg_a), "sandbox")
        eff_b = _resolve_effective_data_dir(None, str(cfg_b), "sandbox")
        assert eff_a == str(tmp_path / "dd_a")
        assert eff_b == str(tmp_path / "dd_b")
        assert eff_a != eff_b

        pid_a = _scoped_pid_path(eff_a)
        pid_b = _scoped_pid_path(eff_b)
        rdy_a = _scoped_readiness_path(eff_a)
        rdy_b = _scoped_readiness_path(eff_b)
        assert pid_a is not None and pid_b is not None and pid_a != pid_b
        assert rdy_a is not None and rdy_b is not None and rdy_a != rdy_b


class TestConfigFileDataDirGate:
    """End-to-end: two sandboxes same HOME via distinct ``--config`` files
    (each config sets a different ``data_dir``, NO ``--data-dir`` flag) →
    both pass the PID gate and write distinct scoped readiness records."""

    def _boot(
        self,
        cfg_path: Path,
        tmp_path: Path,
        monkeypatch,
        captured: list,
    ):
        import functools
        import importlib
        import sys
        import types

        # ``nexus.daemon.main`` the NAME resolves to the click Group
        # (re-exported by the package); the real module is in sys.modules.
        importlib.import_module("nexus.daemon.main")
        dm = sys.modules["nexus.daemon.main"]

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        mock_nx = MagicMock()
        mock_connect = MagicMock(return_value=mock_nx)
        mock_app = MagicMock()
        fake_mod = types.ModuleType("nexus.server.fastapi_server")
        fake_mod.create_app = MagicMock(return_value=mock_app)
        fake_mod.run_server = MagicMock()
        monkeypatch.setitem(sys.modules, "nexus.server.fastapi_server", fake_mod)

        # Capture the ORIGINAL (unspied) writer so a second _boot under the
        # same function-scoped monkeypatch does not chain into the first
        # boot's spy (which would cross-contaminate the capture lists).
        real_write = (
            dm._write_readiness_atomic.__wrapped__
            if hasattr(dm._write_readiness_atomic, "__wrapped__")
            else dm._write_readiness_atomic
        )

        # ``functools.wraps`` sets ``spy_write.__wrapped__ = real_write``
        # (a properly-typed stdlib decorator, so no type suppression is
        # needed) so a SECOND ``_boot`` under the same function-scoped
        # monkeypatch unwraps to the original writer (captured above)
        # instead of chaining onto this spy.
        @functools.wraps(real_write)
        def spy_write(path: Path, host: str, port: int) -> None:
            captured.append(Path(path))
            real_write(path, host, port)

        monkeypatch.setattr(dm, "_write_readiness_atomic", spy_write)

        with patch("nexus.connect", mock_connect):
            return CliRunner().invoke(main, ["--config", str(cfg_path)])

    def test_two_config_files_same_home_both_gate_distinct_scoped(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg_a = tmp_path / "sbx_a.yaml"
        cfg_b = tmp_path / "sbx_b.yaml"
        cfg_a.write_text(f"profile: sandbox\ndata_dir: {tmp_path / 'dd_a'}\n")
        cfg_b.write_text(f"profile: sandbox\ndata_dir: {tmp_path / 'dd_b'}\n")

        cap_a: list = []
        r_a = self._boot(cfg_a, tmp_path, monkeypatch, cap_a)
        assert r_a.exit_code == 0, r_a.output

        cap_b: list = []
        r_b = self._boot(cfg_b, tmp_path, monkeypatch, cap_b)
        # Pre-fix: B's PID gate keyed off the shared legacy global (data_dir
        # was None pre-config-load) ⇒ A's still-fresh global PID could block
        # B; and both scoped readiness writes collapsed to the same global
        # path. Post-fix B passes its own scoped gate.
        assert r_b.exit_code == 0, r_b.output

        # Each boot wrote a SCOPED readiness path distinct from the other's
        # (in addition to the shared legacy global path).
        from nexus.daemon.main import _scoped_readiness_path

        scoped_a = _scoped_readiness_path(str(tmp_path / "dd_a"))
        scoped_b = _scoped_readiness_path(str(tmp_path / "dd_b"))
        assert scoped_a is not None and scoped_b is not None
        assert scoped_a != scoped_b
        assert scoped_a in cap_a, f"A scoped readiness not written: {cap_a}"
        assert scoped_b in cap_b, f"B scoped readiness not written: {cap_b}"
        assert scoped_b not in cap_a and scoped_a not in cap_b


# ---------------------------------------------------------------------------
# _will_use_static_admin_fallback (Issue #4237)
# ---------------------------------------------------------------------------


class TestWillUseStaticAdminFallback:
    """Predicts the "single trusted operator key" boot path (Issue #4237).

    The daemon falls back to ``StaticAPIKeyAuth`` with an implicit
    ``is_admin=True`` principal whenever ``auth_type`` is unset or
    ``"static"`` AND an API key is reachable. In that mode the ReBAC
    filter on the search read path will deny 100% of results unless
    ``allow_admin_bypass=True`` — so this predicate also drives the
    auto-default in ``main()``.
    """

    def test_explicit_static_with_api_key(self, monkeypatch) -> None:
        monkeypatch.delenv("NEXUS_API_KEY_FILE", raising=False)
        assert _will_use_static_admin_fallback("static", "sk-demo") is True

    def test_unset_auth_type_with_api_key(self, monkeypatch) -> None:
        monkeypatch.delenv("NEXUS_API_KEY_FILE", raising=False)
        assert _will_use_static_admin_fallback(None, "sk-demo") is True

    def test_database_auth_does_not_fallback(self, monkeypatch) -> None:
        monkeypatch.delenv("NEXUS_API_KEY_FILE", raising=False)
        assert _will_use_static_admin_fallback("database", "sk-demo") is False

    def test_oidc_auth_does_not_fallback(self, monkeypatch) -> None:
        monkeypatch.delenv("NEXUS_API_KEY_FILE", raising=False)
        assert _will_use_static_admin_fallback("oidc", "sk-demo") is False

    def test_no_api_key_and_no_file(self, monkeypatch) -> None:
        monkeypatch.delenv("NEXUS_API_KEY_FILE", raising=False)
        assert _will_use_static_admin_fallback("static", None) is False

    def test_api_key_file_present(self, tmp_path: Path, monkeypatch) -> None:
        kf = tmp_path / "api.key"
        kf.write_text("sk-from-file")
        monkeypatch.setenv("NEXUS_API_KEY_FILE", str(kf))
        assert _will_use_static_admin_fallback("static", None) is True

    def test_api_key_file_missing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_API_KEY_FILE", str(tmp_path / "absent.key"))
        assert _will_use_static_admin_fallback("static", None) is False


# ---------------------------------------------------------------------------
# _should_default_admin_bypass (Issue #4237)
# ---------------------------------------------------------------------------


class TestShouldDefaultAdminBypass:
    """``allow_admin_bypass`` auto-default decision for static-auth boots.

    Wires the ``_will_use_static_admin_fallback`` predicate with the
    operator-override rules: an explicit ``NEXUS_ALLOW_ADMIN_BYPASS`` env
    or an ``already_set=True`` config-file value defeats the auto-default.
    """

    def _clean_env(self, monkeypatch) -> None:
        """Strip all envs that influence the predicate so each test starts clean."""
        for var in (
            "NEXUS_ALLOW_ADMIN_BYPASS",
            "NEXUS_API_KEY_FILE",
            "NEXUS_DATABASE_URL",
            "POSTGRES_URL",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_static_auth_single_key_defaults_true(self, monkeypatch) -> None:
        self._clean_env(monkeypatch)
        assert _should_default_admin_bypass("static", "sk-demo", already_set=False) is True

    def test_explicit_env_override_blocks_default(self, monkeypatch) -> None:
        """Operator can disable via ``NEXUS_ALLOW_ADMIN_BYPASS=false``."""
        self._clean_env(monkeypatch)
        monkeypatch.setenv("NEXUS_ALLOW_ADMIN_BYPASS", "false")
        assert _should_default_admin_bypass("static", "sk-demo", already_set=False) is False

    def test_env_true_does_not_re_apply(self, monkeypatch) -> None:
        """Env explicitly set to *anything* defers to env precedence."""
        self._clean_env(monkeypatch)
        monkeypatch.setenv("NEXUS_ALLOW_ADMIN_BYPASS", "true")
        assert _should_default_admin_bypass("static", "sk-demo", already_set=False) is False

    def test_config_file_set_blocks_default(self, monkeypatch) -> None:
        """Operator-explicit ``allow_admin_bypass`` in the YAML wins."""
        self._clean_env(monkeypatch)
        assert _should_default_admin_bypass("static", "sk-demo", already_set=True) is False

    def test_database_auth_keeps_secure_default(self, monkeypatch) -> None:
        self._clean_env(monkeypatch)
        assert _should_default_admin_bypass("database", "sk-demo", already_set=False) is False

    def test_no_api_key_no_default(self, monkeypatch) -> None:
        self._clean_env(monkeypatch)
        assert _should_default_admin_bypass("static", None, already_set=False) is False

    def test_db_chain_via_database_url_param_blocks_default(self, monkeypatch) -> None:
        """Round-1 review fix: DB auth chain present → refuse to flip global bypass.

        ``--database-url`` (or ``NEXUS_DATABASE_URL`` / ``POSTGRES_URL`` env)
        means ``_ChainedAPIKeyAuth(static, db)`` admits DB-stored admin keys
        too. A global ``allow_admin_bypass`` would silently weaken ReBAC for
        those keys, defeating #3063.
        """
        self._clean_env(monkeypatch)
        assert (
            _should_default_admin_bypass(
                "static",
                "sk-demo",
                already_set=False,
                database_url="postgresql://localhost/nexus",
            )
            is False
        )

    def test_db_chain_via_env_blocks_default(self, monkeypatch) -> None:
        self._clean_env(monkeypatch)
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://localhost/nexus")
        assert _should_default_admin_bypass("static", "sk-demo", already_set=False) is False

    def test_postgres_url_env_blocks_default(self, monkeypatch) -> None:
        """``POSTGRES_URL`` env is the legacy alias the daemon's auth path
        also consumes — same chaining risk."""
        self._clean_env(monkeypatch)
        monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/nexus")
        assert _should_default_admin_bypass("static", "sk-demo", already_set=False) is False
