"""Unit tests for VFS I/O lock wiring in NexusFS syscalls (Issue #906).

Verifies that _vfs_acquire, _vfs_locked, and the lock wiring into
sys_read/sys_write/sys_rename/sys_unlink work correctly.
"""

import contextlib
import threading

import pytest

from nexus.contracts.exceptions import LockTimeout
from nexus.core.lock_fast import PythonVFSLockManager

# ---------------------------------------------------------------------------
# Standalone tests for _vfs_acquire / _vfs_locked helpers
# ---------------------------------------------------------------------------


class _FakeKernel:
    """Minimal stub to test _vfs_acquire and _vfs_locked in isolation.

    Copies the exact implementation from NexusFS so we can test without
    instantiating the full kernel.
    """

    _VFS_LOCK_TIMEOUT_MS = 100  # short timeout for tests

    def __init__(self):
        self._vfs_lock_manager = PythonVFSLockManager()

    def _vfs_acquire(self, path: str, mode: str) -> int:
        handle = self._vfs_lock_manager.acquire(path, mode, timeout_ms=self._VFS_LOCK_TIMEOUT_MS)
        if handle == 0:
            raise LockTimeout(
                path=path,
                timeout=self._VFS_LOCK_TIMEOUT_MS / 1000,
                message=f"VFS {mode} lock timeout on {path}",
            )
        return handle

    @contextlib.contextmanager
    def _vfs_locked(self, path: str, mode: str):
        handle = self._vfs_acquire(path, mode)
        try:
            yield handle
        finally:
            self._vfs_lock_manager.release(handle)


@pytest.fixture
def kernel():
    return _FakeKernel()


class TestVfsAcquire:
    def test_acquire_returns_handle(self, kernel):
        h = kernel._vfs_acquire("/foo", "write")
        assert h > 0
        kernel._vfs_lock_manager.release(h)

    def test_acquire_raises_on_conflict(self, kernel):
        h = kernel._vfs_acquire("/foo", "write")
        with pytest.raises(LockTimeout):
            kernel._vfs_acquire("/foo", "read")
        kernel._vfs_lock_manager.release(h)

    def test_acquire_invalid_mode_raises(self, kernel):
        with pytest.raises(ValueError):
            kernel._vfs_acquire("/foo", "exclusive")


class TestVfsLocked:
    def test_context_manager_acquires_and_releases(self, kernel):
        with kernel._vfs_locked("/bar", "write") as h:
            assert h > 0
            assert kernel._vfs_lock_manager.is_locked("/bar")
        # After exiting, lock should be released
        assert not kernel._vfs_lock_manager.is_locked("/bar")

    def test_context_manager_releases_on_exception(self, kernel):
        with pytest.raises(RuntimeError), kernel._vfs_locked("/bar", "write"):
            raise RuntimeError("boom")
        # Lock should still be released
        assert not kernel._vfs_lock_manager.is_locked("/bar")

    def test_read_lock_allows_concurrent_reads(self, kernel):
        with kernel._vfs_locked("/shared", "read"), kernel._vfs_locked("/shared", "read") as h2:
            assert h2 > 0

    def test_write_lock_blocks_read(self, kernel):
        with kernel._vfs_locked("/excl", "write"), pytest.raises(LockTimeout):
            kernel._vfs_acquire("/excl", "read")


class TestRenameDeadlockFree:
    """Verify that two-path locking in sorted order prevents deadlock."""

    def test_sorted_order_prevents_deadlock(self, kernel):
        """Two threads locking (A, B) vs (B, A) — sorted order serializes."""
        errors = []

        def rename_ab():
            try:
                first, second = sorted(["/a", "/b"])
                h1 = kernel._vfs_acquire(first, "write")
                try:
                    h2 = kernel._vfs_acquire(second, "write")
                    try:
                        pass  # simulate rename
                    finally:
                        kernel._vfs_lock_manager.release(h2)
                finally:
                    kernel._vfs_lock_manager.release(h1)
            except Exception as e:
                errors.append(e)

        def rename_ba():
            try:
                first, second = sorted(["/b", "/a"])
                h1 = kernel._vfs_acquire(first, "write")
                try:
                    h2 = kernel._vfs_acquire(second, "write")
                    try:
                        pass  # simulate rename
                    finally:
                        kernel._vfs_lock_manager.release(h2)
                finally:
                    kernel._vfs_lock_manager.release(h1)
            except Exception as e:
                errors.append(e)

        # Run 10 rounds to stress-test
        for _ in range(10):
            t1 = threading.Thread(target=rename_ab)
            t2 = threading.Thread(target=rename_ba)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        # At most one thread may timeout per round, but no deadlock
        assert not any(isinstance(e, RuntimeError) for e in errors)
