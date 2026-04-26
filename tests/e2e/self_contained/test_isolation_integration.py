"""Integration tests — full round-trip through IsolatedBackend with real pool.

Uses ``ProcessPoolExecutor`` (``force_process=True``) for portability.
"""

import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

# TODO(post-#3890): see ``test_isolation_boundary.py`` — same root
# cause (CI hang in E2E Self-Contained step), same skip rationale.
pytestmark = pytest.mark.skip(
    reason="Hangs CI E2E Self-Contained step on PR #3890 — investigate post-merge",
)

from nexus.bricks.sandbox.isolation import IsolatedBackend, IsolationConfig  # noqa: E402

_MOD = "tests.e2e.self_contained.isolation_helpers"
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
def backend(tmp_path) -> Iterator[IsolatedBackend]:
    b = IsolatedBackend(_cfg(backend_kwargs={"storage_dir": str(tmp_path / "store")}))
    yield b
    b.close()


class TestFullRoundTrip:
    def test_write_read_delete_cycle(self, backend: IsolatedBackend) -> None:
        data = b"integration-roundtrip"
        wr = backend.write_content(data)
        assert wr.content_id  # WriteResult with content_hash

        rd = backend.read_content(wr.content_id)
        assert rd == data

        backend.delete_content(wr.content_id)

        ex = backend.content_exists(wr.content_id)
        assert ex is False

    def test_directory_operations(self, backend: IsolatedBackend) -> None:
        backend.mkdir("/integration-dir", exist_ok=True)

        is_dir = backend.is_directory("/integration-dir")
        assert is_dir is True

        backend.rmdir("/integration-dir")


class TestLargeContent:
    def test_1mb_transfer(self, backend: IsolatedBackend) -> None:
        """1MB content passes through the isolation boundary correctly."""
        data = b"X" * (1024 * 1024)
        wr = backend.write_content(data)
        assert wr.content_id

        rd = backend.read_content(wr.content_id)
        assert len(rd) == len(data)
        assert rd == data


class TestConcurrentRequests:
    def test_parallel_reads(self, backend: IsolatedBackend) -> None:
        """10 parallel reads via ThreadPoolExecutor do not deadlock."""
        # Write one piece of content
        data = b"concurrent-test"
        wr = backend.write_content(data)
        content_hash = wr.content_id

        def read_one(_: int) -> bool:
            rd = backend.read_content(content_hash)
            return rd == data

        with ThreadPoolExecutor(max_workers=5) as tp:
            results = list(tp.map(read_one, range(10)))

        assert all(results)


class TestPoolRestartRecovery:
    def test_recovery_after_restart(self) -> None:
        """After pool restart (max failures), a valid call still succeeds."""
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(
                max_consecutive_failures=2,
                backend_kwargs={"storage_dir": td + "/store"},
            )
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
                assert wr.content_id  # WriteResult with content_hash
            finally:
                backend.close()


class TestShutdownAndReconnect:
    def test_close_then_error(self) -> None:
        """After close, calls raise BackendError (not crash)."""
        from nexus.contracts.exceptions import BackendError

        with tempfile.TemporaryDirectory() as td:
            b = IsolatedBackend(_cfg(backend_kwargs={"storage_dir": td + "/store"}))
            b.close()
            with pytest.raises(BackendError):
                b.write_content(b"after-shutdown")
