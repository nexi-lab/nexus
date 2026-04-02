"""VFS Lock Manager with Rust acceleration (Issue #1398).

Provides path-level read/write locking with hierarchical awareness.
This is a local, in-process lock manager — it does NOT replace the
distributed Raft-based lock system (``distributed_lock.py``).

Fallback chain:
    1. Rust ``VFSLockManager`` (via ``nexus_fast``) — ~100-200ns per acquire
    2. Python ``PythonVFSLockManager`` (threading-based) — ~500ns-1us
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Python fallback
# ---------------------------------------------------------------------------


class _LockEntry:
    __slots__ = ("readers", "writer")

    def __init__(self) -> None:
        self.readers: int = 0
        self.writer: int | None = None

    def is_idle(self) -> bool:
        return self.readers == 0 and self.writer is None


def _normalize_path(path: str) -> str:
    """Normalize a path: resolve ./.., collapse repeated slashes, remove trailing slash."""
    if not path:
        return "/"
    import posixpath

    result = posixpath.normpath(path)
    # posixpath.normpath preserves leading // per POSIX; collapse to single /
    if result.startswith("//"):
        result = result[1:]
    # Ensure leading slash is preserved for absolute paths
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result


def _ancestors(path: str) -> list[str]:
    """Return strict ancestors of *path* (deepest first). Assumes normalized input."""
    if path == "/" or not path:
        return []
    result: list[str] = []
    end = len(path)
    while True:
        pos = path.rfind("/", 0, end)
        if pos < 0:
            break
        if pos == 0:
            result.append("/")
            break
        result.append(path[:pos])
        end = pos
    return result


class PythonVFSLockManager:
    """Pure-Python fallback using ``threading.RLock`` + dict."""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._locks: dict[str, _LockEntry] = {}
        self._handles: dict[int, tuple[str, str]] = {}  # handle -> (path, mode)
        self._next_handle = 1

        # Metrics
        self._acquire_count = 0
        self._release_count = 0
        self._contention_count = 0
        self._timeout_count = 0
        self._total_acquire_ns = 0

    # -- helpers -----------------------------------------------------------

    def _ancestor_conflict(self, path: str, mode: str) -> bool:
        for anc in _ancestors(path):
            entry = self._locks.get(anc)
            if entry is None:
                continue
            if mode == "read" and entry.writer is not None:
                return True
            if mode == "write" and (entry.writer is not None or entry.readers > 0):
                return True
        return False

    def _descendant_conflict(self, path: str, mode: str) -> bool:
        prefix = path if path.endswith("/") else path + "/"
        for key, entry in self._locks.items():
            if not key.startswith(prefix):
                continue
            if mode == "read" and entry.writer is not None:
                return True
            if mode == "write" and (entry.writer is not None or entry.readers > 0):
                return True
        return False

    def _try_acquire_once(self, path: str, mode: str) -> int:
        """Non-blocking single attempt.  Returns handle or 0."""
        with self._mu:
            if self._ancestor_conflict(path, mode):
                return 0
            if self._descendant_conflict(path, mode):
                return 0

            entry = self._locks.get(path)
            if entry is None:
                entry = _LockEntry()
                self._locks[path] = entry

            if mode == "read":
                if entry.writer is not None:
                    return 0
                handle = self._next_handle
                self._next_handle += 1
                entry.readers += 1
                self._handles[handle] = (path, mode)
                return handle

            # mode == "write"
            if entry.writer is not None or entry.readers > 0:
                return 0
            handle = self._next_handle
            self._next_handle += 1
            entry.writer = handle
            self._handles[handle] = (path, mode)
            return handle

    # -- public API --------------------------------------------------------

    def acquire(self, path: str, mode: str, timeout_ms: int = 0) -> int:
        if mode not in ("read", "write"):
            raise ValueError(f'Invalid lock mode: {mode!r}. Expected "read" or "write".')

        path = _normalize_path(path)
        start_ns = time.perf_counter_ns()

        handle = self._try_acquire_once(path, mode)
        if handle:
            elapsed = time.perf_counter_ns() - start_ns
            self._total_acquire_ns += elapsed
            self._acquire_count += 1
            return handle

        if timeout_ms == 0:
            self._contention_count += 1
            self._timeout_count += 1
            return 0

        deadline = time.monotonic() + timeout_ms / 1000.0

        # Use condition variable instead of busy-wait spin loop.
        # Waiters are notified on lock release for immediate wakeup.
        if not hasattr(self, "_cv"):
            import threading

            self._cv = threading.Condition(threading.Lock())

        while True:
            with self._cv:
                self._cv.wait(timeout=0.005)  # max 5ms between checks
            self._contention_count += 1

            handle = self._try_acquire_once(path, mode)
            if handle:
                elapsed = time.perf_counter_ns() - start_ns
                self._total_acquire_ns += elapsed
                self._acquire_count += 1
                return handle

            if time.monotonic() >= deadline:
                self._timeout_count += 1
                return 0

    def release(self, handle: int) -> bool:
        with self._mu:
            info = self._handles.pop(handle, None)
            if info is None:
                return False

            path, mode = info
            entry = self._locks.get(path)
            if entry is not None:
                if mode == "read":
                    entry.readers = max(0, entry.readers - 1)
                elif mode == "write" and entry.writer == handle:
                    entry.writer = None

                if entry.is_idle():
                    del self._locks[path]

            self._release_count += 1

            # Wake up any waiters blocked in acquire()
            if hasattr(self, "_cv"):
                with self._cv:
                    self._cv.notify_all()

            return True

    def is_locked(self, path: str) -> bool:
        path = _normalize_path(path)
        with self._mu:
            entry = self._locks.get(path)
            return entry is not None and not entry.is_idle()

    def holders(self, path: str) -> dict | None:
        path = _normalize_path(path)
        with self._mu:
            entry = self._locks.get(path)
            if entry is None or entry.is_idle():
                return None
            return {
                "readers": entry.readers,
                "writer": entry.writer or 0,
                "path": path,
            }

    def stats(self) -> dict:
        acquires = self._acquire_count
        avg_ns = self._total_acquire_ns // acquires if acquires else 0
        with self._mu:
            active = len(self._locks)
            handles = len(self._handles)
        return {
            "acquire_count": acquires,
            "release_count": self._release_count,
            "contention_count": self._contention_count,
            "timeout_count": self._timeout_count,
            "active_locks": active,
            "active_handles": handles,
            "avg_acquire_ns": avg_ns,
            "total_acquire_ns": self._total_acquire_ns,
        }

    @property
    def active_locks(self) -> int:
        with self._mu:
            return len(self._locks)


# ---------------------------------------------------------------------------
# Rust wrapper
# ---------------------------------------------------------------------------


# RUST_FALLBACK: VFSLockManager
class RustVFSLockManager:
    """Thin wrapper around ``nexus_fast.VFSLockManager``."""

    def __init__(self) -> None:
        from nexus_fast import VFSLockManager

        self._inner = VFSLockManager()
        # Expose Rust PyO3 object for Arc sharing with SyscallEngine (Phase G)
        self._rust = self._inner

    def acquire(self, path: str, mode: str, timeout_ms: int = 0) -> int:
        return self._inner.acquire(path, mode, timeout_ms)

    def release(self, handle: int) -> bool:
        return self._inner.release(handle)

    def is_locked(self, path: str) -> bool:
        return self._inner.is_locked(path)

    def holders(self, path: str) -> dict | None:
        return self._inner.holders(path)

    def stats(self) -> dict:
        return self._inner.stats()

    @property
    def active_locks(self) -> int:
        return self._inner.active_locks


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_vfs_lock_manager() -> RustVFSLockManager | PythonVFSLockManager:
    """Return the best available VFS lock manager.

    Prefers the Rust implementation; falls back to pure Python.
    """
    try:
        mgr = RustVFSLockManager()
        logger.debug("VFS lock manager: Rust (nexus_fast)")
        return mgr
    except (ImportError, Exception) as exc:
        logger.debug("Rust VFS lock manager unavailable (%s), using Python fallback", exc)
        return PythonVFSLockManager()
