"""Integration tests — full round-trip through IsolatedBackend with real pool.

Uses ``ProcessPoolExecutor`` (``force_process=True``) for portability.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from nexus.isolation import IsolatedBackend, IsolationConfig

_MOD = "tests.unit.isolation.conftest"
_CLS = "MockBackend"


def _cfg(**overrides: object) -> IsolationConfig:
    defaults: dict = {
        "backend_module": _MOD,
        "backend_class": _CLS,
        "pool_size": 2,
        "call_timeout": 30.0,
        "startup_timeout": 30.0,
        "force_process": True,
    }
    defaults.update(overrides)
    return IsolationConfig(**defaults)


@pytest.fixture()
def backend() -> IsolatedBackend:
    b = IsolatedBackend(_cfg())
    yield b  # type: ignore[misc]
    b.disconnect()


class TestFullRoundTrip:
    def test_write_read_delete_cycle(self, backend: IsolatedBackend) -> None:
        data = b"integration-roundtrip"
        wr = backend.write_content(data)
        assert wr.success is True

        rd = backend.read_content(wr.data)
        assert rd.success is True
        assert rd.data == data

        dl = backend.delete_content(wr.data)
        assert dl.success is True

        ex = backend.content_exists(wr.data)
        assert ex.data is False

    def test_directory_operations(self, backend: IsolatedBackend) -> None:
        mk = backend.mkdir("/integration-dir", exist_ok=True)
        assert mk.success is True

        is_dir = backend.is_directory("/integration-dir")
        assert is_dir.data is True

        rm = backend.rmdir("/integration-dir")
        assert rm.success is True


class TestLargeContent:
    def test_1mb_transfer(self, backend: IsolatedBackend) -> None:
        """1MB content passes through the isolation boundary correctly."""
        data = b"X" * (1024 * 1024)
        wr = backend.write_content(data)
        assert wr.success is True

        rd = backend.read_content(wr.data)
        assert rd.success is True
        assert len(rd.data) == len(data)
        assert rd.data == data


class TestConcurrentRequests:
    def test_parallel_reads(self, backend: IsolatedBackend) -> None:
        """10 parallel reads via ThreadPoolExecutor do not deadlock."""
        # Write one piece of content
        data = b"concurrent-test"
        wr = backend.write_content(data)
        content_hash = wr.data

        def read_one(_: int) -> bool:
            rd = backend.read_content(content_hash)
            return rd.success and rd.data == data

        with ThreadPoolExecutor(max_workers=5) as tp:
            results = list(tp.map(read_one, range(10)))

        assert all(results)


class TestPoolRestartRecovery:
    def test_recovery_after_restart(self) -> None:
        """After pool restart (max failures), a valid call still succeeds."""
        cfg = _cfg(max_consecutive_failures=2)
        backend = IsolatedBackend(cfg)
        try:
            # Force failures by calling a method that raises in worker
            for _ in range(2):
                try:
                    backend.list_dir("/nonexistent")
                except FileNotFoundError:
                    pass
            # Pool should have restarted — next valid call should work
            wr = backend.write_content(b"after-restart")
            assert wr.success is True
        finally:
            backend.disconnect()


class TestShutdownAndReconnect:
    def test_disconnect_then_error(self) -> None:
        """After disconnect, calls return error (not crash)."""
        b = IsolatedBackend(_cfg())
        b.disconnect()
        resp = b.write_content(b"after-shutdown")
        assert resp.success is False
