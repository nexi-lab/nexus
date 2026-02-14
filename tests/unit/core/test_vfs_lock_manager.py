"""Unit tests for VFS Lock Manager (Issue #1398).

Tests both the Rust-accelerated and pure-Python implementations to verify
identical semantics.
"""

from __future__ import annotations

import threading
import time

import pytest

from nexus.core.lock_fast import (
    PythonVFSLockManager,
    VFSLockManagerProtocol,
    create_vfs_lock_manager,
)

# ---------------------------------------------------------------------------
# Fixtures — parametrize over both implementations
# ---------------------------------------------------------------------------

_IMPLEMENTATIONS: list[type] = [PythonVFSLockManager]

try:
    from nexus.core.lock_fast import RustVFSLockManager

    _IMPLEMENTATIONS.append(RustVFSLockManager)
except (ImportError, Exception):
    pass


@pytest.fixture(params=_IMPLEMENTATIONS, ids=lambda cls: cls.__name__)
def mgr(request: pytest.FixtureRequest) -> VFSLockManagerProtocol:
    return request.param()


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


class TestBasicAcquireRelease:
    def test_read_acquire_release(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/foo", "read")
        assert h > 0
        assert mgr.is_locked("/foo")
        assert mgr.release(h)
        assert not mgr.is_locked("/foo")

    def test_write_acquire_release(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/foo", "write")
        assert h > 0
        assert mgr.is_locked("/foo")
        assert mgr.release(h)
        assert not mgr.is_locked("/foo")

    def test_invalid_mode_raises(self, mgr: VFSLockManagerProtocol) -> None:
        with pytest.raises((ValueError, Exception)):
            mgr.acquire("/foo", "exclusive")


# ---------------------------------------------------------------------------
# Read-read coexistence
# ---------------------------------------------------------------------------


class TestReadReadCoexistence:
    def test_two_readers_same_path(self, mgr: VFSLockManagerProtocol) -> None:
        h1 = mgr.acquire("/foo", "read")
        h2 = mgr.acquire("/foo", "read")
        assert h1 > 0
        assert h2 > 0
        assert h1 != h2
        assert mgr.is_locked("/foo")
        mgr.release(h1)
        assert mgr.is_locked("/foo")
        mgr.release(h2)
        assert not mgr.is_locked("/foo")


# ---------------------------------------------------------------------------
# Read-write conflicts
# ---------------------------------------------------------------------------


class TestReadWriteConflict:
    def test_write_blocks_read(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/foo", "write")
        assert w > 0
        r = mgr.acquire("/foo", "read")  # non-blocking → should fail
        assert r == 0
        mgr.release(w)

    def test_read_blocks_write(self, mgr: VFSLockManagerProtocol) -> None:
        r = mgr.acquire("/foo", "read")
        assert r > 0
        w = mgr.acquire("/foo", "write")
        assert w == 0
        mgr.release(r)

    def test_write_write_conflict(self, mgr: VFSLockManagerProtocol) -> None:
        w1 = mgr.acquire("/foo", "write")
        assert w1 > 0
        w2 = mgr.acquire("/foo", "write")
        assert w2 == 0
        mgr.release(w1)


# ---------------------------------------------------------------------------
# Ancestor conflicts
# ---------------------------------------------------------------------------


class TestAncestorConflict:
    def test_ancestor_write_blocks_child_read(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/a", "write")
        assert mgr.acquire("/a/b", "read") == 0
        mgr.release(w)

    def test_ancestor_write_blocks_child_write(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/a", "write")
        assert mgr.acquire("/a/b/c", "write") == 0
        mgr.release(w)

    def test_ancestor_read_allows_child_read(self, mgr: VFSLockManagerProtocol) -> None:
        r = mgr.acquire("/a", "read")
        child = mgr.acquire("/a/b", "read")
        assert child > 0
        mgr.release(r)
        mgr.release(child)

    def test_ancestor_read_blocks_child_write(self, mgr: VFSLockManagerProtocol) -> None:
        r = mgr.acquire("/a", "read")
        assert mgr.acquire("/a/b", "write") == 0
        mgr.release(r)

    def test_root_write_blocks_all_descendants(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/", "write")
        assert w > 0
        assert mgr.acquire("/a", "read") == 0
        assert mgr.acquire("/a/b/c", "write") == 0
        mgr.release(w)

    def test_descendant_blocks_root_write(self, mgr: VFSLockManagerProtocol) -> None:
        r = mgr.acquire("/a", "read")
        assert mgr.acquire("/", "write") == 0
        mgr.release(r)


# ---------------------------------------------------------------------------
# Descendant conflicts
# ---------------------------------------------------------------------------


class TestDescendantConflict:
    def test_descendant_write_blocks_parent_write(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/a/b/c", "write")
        assert mgr.acquire("/a", "write") == 0
        mgr.release(w)

    def test_descendant_read_blocks_parent_write(self, mgr: VFSLockManagerProtocol) -> None:
        r = mgr.acquire("/a/b", "read")
        assert mgr.acquire("/a", "write") == 0
        mgr.release(r)

    def test_descendant_write_blocks_parent_read(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/a/b", "write")
        assert mgr.acquire("/a", "read") == 0
        mgr.release(w)


# ---------------------------------------------------------------------------
# Release wrong handle
# ---------------------------------------------------------------------------


class TestReleaseEdgeCases:
    def test_release_invalid_handle(self, mgr: VFSLockManagerProtocol) -> None:
        assert not mgr.release(999)

    def test_double_release(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/foo", "write")
        assert mgr.release(h)
        assert not mgr.release(h)


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_nonblocking_returns_zero(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/foo", "write")
        result = mgr.acquire("/foo", "read", timeout_ms=0)
        assert result == 0
        mgr.release(w)

    def test_blocking_timeout_returns_zero(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/foo", "write")
        start = time.monotonic()
        result = mgr.acquire("/foo", "read", timeout_ms=50)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert result == 0
        assert elapsed_ms >= 40  # allow some slack
        mgr.release(w)

    def test_blocking_succeeds_when_released(self, mgr: VFSLockManagerProtocol) -> None:
        w = mgr.acquire("/foo", "write")
        result_holder: list[int] = []

        def release_later() -> None:
            time.sleep(0.02)
            mgr.release(w)

        t = threading.Thread(target=release_later)
        t.start()

        # Should succeed within 500ms timeout.
        r = mgr.acquire("/foo", "read", timeout_ms=500)
        result_holder.append(r)
        t.join()

        assert result_holder[0] > 0
        mgr.release(result_holder[0])


# ---------------------------------------------------------------------------
# Holders info
# ---------------------------------------------------------------------------


class TestHolders:
    def test_holders_none_when_unlocked(self, mgr: VFSLockManagerProtocol) -> None:
        assert mgr.holders("/foo") is None

    def test_holders_shows_readers(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/foo", "read")
        info = mgr.holders("/foo")
        assert info is not None
        assert info["readers"] == 1
        mgr.release(h)

    def test_holders_shows_writer(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/foo", "write")
        info = mgr.holders("/foo")
        assert info is not None
        assert info["writer"] == h
        mgr.release(h)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_keys(self, mgr: VFSLockManagerProtocol) -> None:
        s = mgr.stats()
        expected_keys = {
            "acquire_count",
            "release_count",
            "contention_count",
            "timeout_count",
            "active_locks",
            "active_handles",
            "avg_acquire_ns",
            "total_acquire_ns",
        }
        assert expected_keys.issubset(set(s.keys()))

    def test_stats_after_operations(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/x", "read")
        mgr.release(h)
        s = mgr.stats()
        assert s["acquire_count"] >= 1
        assert s["release_count"] >= 1


# ---------------------------------------------------------------------------
# Unicode paths
# ---------------------------------------------------------------------------


class TestUnicodePaths:
    def test_unicode_path(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/datos/archivo", "write")
        assert h > 0
        assert mgr.is_locked("/datos/archivo")
        mgr.release(h)

    def test_cjk_path(self, mgr: VFSLockManagerProtocol) -> None:
        h = mgr.acquire("/data/file", "read")
        assert h > 0
        mgr.release(h)


# ---------------------------------------------------------------------------
# Concurrent threading test
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_readers(self, mgr: VFSLockManagerProtocol) -> None:
        """10 threads each acquire a read lock — all should succeed."""
        handles: list[int] = []
        errors: list[Exception] = []

        def reader() -> None:
            try:
                h = mgr.acquire("/shared", "read", timeout_ms=1000)
                if h > 0:
                    handles.append(h)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert len(handles) == 10

        for h in handles:
            mgr.release(h)

    def test_concurrent_writers_exclusive(self, mgr: VFSLockManagerProtocol) -> None:
        """10 threads try to acquire a write lock — exactly one succeeds (non-blocking)."""
        successes: list[int] = []

        def writer() -> None:
            h = mgr.acquire("/exclusive", "write", timeout_ms=0)
            if h > 0:
                successes.append(h)

        threads = [threading.Thread(target=writer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(successes) == 1
        mgr.release(successes[0])


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestFactory:
    def test_create_returns_protocol(self) -> None:
        mgr = create_vfs_lock_manager()
        assert isinstance(mgr, VFSLockManagerProtocol)

    def test_factory_functional(self) -> None:
        mgr = create_vfs_lock_manager()
        h = mgr.acquire("/test", "write")
        assert h > 0
        assert mgr.release(h)
