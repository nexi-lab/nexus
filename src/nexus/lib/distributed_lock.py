"""Advisory lock manager — ABCs and LocalLockManager.

Advisory locks are *metadata* — visible, queryable, TTL-based.
Used for user/service coordination (task queues, turn-taking, resource
contention).  Distinct from kernel I/O locks (VFSLockManager, ~200ns,
in-memory, process-scoped).

Architecture:
- AdvisoryLockManager: Sync advisory lock API (POSIX flock(2))
- LocalLockManager: Standalone mode — wraps VFSSemaphore (this file)
- RaftLockManager: Federation mode — wraps RaftMetadataStore (raft/)

Lock keys are zone-canonical paths (e.g. ``/root/workspace/file.txt``)
constructed by PathRouter. Lock managers are zone-agnostic — they receive
canonical paths and use them as-is.

NexusFS kernel auto-creates LocalLockManager (standalone) or receives
RaftLockManager (federation) via _upgrade_lock_manager().
Callers see only AdvisoryLockManager.

References:
    - docs/architecture/lock-architecture.md
    - docs/architecture/federation-memo.md §6.9
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class LockInfo:
    """Information about a lock on a resource.

    Returned by get_lock_info() and list_locks().
    """

    path: str
    mode: Literal["mutex", "semaphore"]
    max_holders: int
    holders: "list[HolderInfo]"
    fence_token: int


@dataclass
class HolderInfo:
    """Information about a single lock holder."""

    lock_id: str
    holder_info: str
    acquired_at: float  # Unix timestamp
    expires_at: float  # Unix timestamp


@dataclass
class ExtendResult:
    """Result of a lock extend (heartbeat) operation."""

    success: bool
    lock_info: LockInfo | None = None


# =============================================================================
# Abstract Base Class
# =============================================================================


class AdvisoryLockManager(ABC):
    """Abstract base class for advisory lock manager implementations.

    POSIX analogue: ``flock(2)`` — process-associated advisory file locks.
    The ``mode`` parameter selects between exclusive (``LOCK_EX``) and shared
    (``LOCK_SH``) semantics.  When ``max_holders > 1``, the lock degrades to
    a counting semaphore (no exclusive/shared distinction).

    Lock keys are zone-canonical paths (``/root/workspace/file.txt``)
    constructed by PathRouter. Lock managers are zone-agnostic.

    All methods are sync — blocking waits are handled by Rust Condvar
    (GIL released by PyO3) or equivalent blocking primitive.

    Subclasses must implement all abstract methods.
    """

    DEFAULT_TTL = 30.0  # Default lock TTL in seconds
    DEFAULT_TIMEOUT = 30.0  # Default acquisition timeout

    def _lock_key(self, path: str) -> str:
        """Return store-level lock key. Identity — path is already canonical."""
        return path

    @abstractmethod
    def acquire(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire an advisory lock or semaphore slot.

        Args:
            path: Path to lock
            mode: Lock mode — ``"exclusive"`` (LOCK_EX, default) blocks all
                  other holders; ``"shared"`` (LOCK_SH) allows concurrent
                  readers but blocks exclusive writers.  Ignored when
                  ``max_holders > 1`` (counting semaphore).
            timeout: Maximum time to wait for acquisition (seconds)
            ttl: Lock TTL — auto-expires after this duration (seconds)
            max_holders: Maximum concurrent holders.  ``1`` = mutex (default),
                         ``> 1`` = counting semaphore (mode is ignored).

        Returns:
            Lock ID (UUID) if acquired, ``None`` on timeout.
        """

    @abstractmethod
    def release(
        self,
        lock_id: str,
        path: str,
    ) -> bool:
        """Release an advisory lock."""

    @abstractmethod
    def extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = DEFAULT_TTL,
    ) -> ExtendResult:
        """Extend lock TTL (heartbeat)."""

    @abstractmethod
    def get_lock_info(
        self,
        path: str,
    ) -> LockInfo | None:
        """Get information about a lock."""

    def is_locked(self, path: str) -> bool:
        """Check if a path is currently locked. Override for efficiency."""
        info = self.get_lock_info(path)
        return info is not None

    @abstractmethod
    def list_locks(
        self,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks."""

    @abstractmethod
    def force_release(
        self,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation)."""

    @abstractmethod
    def health_check(self) -> bool:
        """Check if the lock manager is healthy."""


# =============================================================================
# Concrete: LocalLockManager (standalone mode)
# =============================================================================

_SHARED_MAX_HOLDERS = 1024  # max concurrent shared (reader) holders


class LocalLockManager(AdvisoryLockManager):
    """Advisory lock manager for standalone mode — wraps VFSSemaphore.

    Uses kernel ``VFSSemaphore`` primitive directly instead of going
    through MetastoreABC lock methods.

    Supports three modes:

    - **exclusive** (``max_holders=1, mode="exclusive"``): Mutex.  Single
      holder via gate semaphore (``{key}:gate``, ``max_holders=1``).  Writer
      holds gate for the entire critical section and waits for active readers
      to drain before proceeding.
    - **shared** (``max_holders=1, mode="shared"``): Reader-writer lock.
      Reader briefly acquires gate, takes a slot on the readers semaphore
      (``{key}:readers``, ``max_holders=1024``), then releases gate so other
      readers can enter.  An exclusive writer blocks new readers by holding
      gate.
    - **counting** (``max_holders > 1``): Simple counting semaphore mapped
      directly to ``VFSSemaphore(name, max_holders=N)``.  ``mode`` is ignored.

    All blocking waits are handled by Rust Condvar (GIL released by PyO3).
    No asyncio.sleep polling — direct blocking acquire with timeout.
    """

    RETRY_INTERVAL = 0.05  # 50ms between retries (gate pattern fallback only)

    # Prefix for _active_locks entries to distinguish lock_fast vs semaphore
    _VFS_LOCK = "vfs"
    _SEM_LOCK = "sem"

    def __init__(
        self,
        semaphore: Any,
        *,
        vfs_lock_manager: Any = None,
    ) -> None:
        super().__init__()
        self._sem = semaphore
        self._vfs_lock = vfs_lock_manager  # lock_fast: ~200ns RW lock for exclusive/shared
        # lock_id → (lock_type, handle_or_holder_id) for release
        self._active_locks: dict[str, tuple[str, Any]] = {}

    # -- acquire modes --------------------------------------------------------

    def acquire(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",
        timeout: float = AdvisoryLockManager.DEFAULT_TIMEOUT,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        from nexus.lib.lock_order import L2_ADVISORY, assert_can_acquire, mark_acquired

        assert_can_acquire(L2_ADVISORY)
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        key = self._lock_key(path)
        ttl_ms = max(1000, int(ttl * 1000))
        lock_id = str(uuid.uuid4())
        timeout_ms = max(0, int(timeout * 1000))

        if max_holders > 1:
            # Counting semaphore — blocking acquire via Rust Condvar (GIL released)
            holder = self._sem.acquire(
                key, max_holders=max_holders, timeout_ms=timeout_ms, ttl_ms=ttl_ms
            )
            if holder is not None:
                self._active_locks[lock_id] = (self._SEM_LOCK, (key, holder))
                mark_acquired(L2_ADVISORY)
                logger.debug(
                    "Counting lock acquired: %s -> %s (max_holders=%d)", key, lock_id, max_holders
                )
                return lock_id
            return None

        # Exclusive/shared (max_holders=1): prefer lock_fast (~200ns) over gate pattern
        if self._vfs_lock is not None:
            return self._acquire_via_vfs_lock(key, lock_id, mode, timeout_ms)

        # Fallback: VFSSemaphore gate pattern
        if mode == "exclusive":
            return self._acquire_exclusive(key, lock_id, timeout, ttl_ms)
        else:  # shared
            return self._acquire_shared(key, lock_id, timeout, ttl_ms)

    def _acquire_via_vfs_lock(
        self,
        key: str,
        lock_id: str,
        mode: str,
        timeout_ms: int,
    ) -> str | None:
        """Acquire exclusive/shared via lock_fast RW lock (~200ns).

        Blocking acquire with timeout handled by Rust Condvar (GIL released).
        """
        from nexus.lib.lock_order import L2_ADVISORY, mark_acquired

        vfs_mode = "write" if mode == "exclusive" else "read"
        handle: int = self._vfs_lock.acquire(key, vfs_mode, timeout_ms=timeout_ms)
        if handle:
            self._active_locks[lock_id] = (self._VFS_LOCK, (key, handle))
            mark_acquired(L2_ADVISORY)
            logger.debug("%s lock acquired (lock_fast): %s -> %s", mode.title(), key, lock_id)
            return lock_id
        logger.debug("%s lock timeout (lock_fast): %s", mode.title(), key)
        return None

    def _acquire_exclusive(
        self,
        key: str,
        lock_id: str,
        timeout: float,
        ttl_ms: int,
    ) -> str | None:
        """Writer: hold gate for duration, wait for readers to drain.

        Uses blocking acquire with timeout via Rust Condvar for gate.
        Readers drain check uses sync polling (readers semaphore has no
        "wait until zero" primitive).
        """
        gate = f"{key}:gate"
        readers = f"{key}:readers"
        deadline = time.monotonic() + timeout
        timeout_ms = max(0, int(timeout * 1000))

        # Step 1: acquire gate with blocking timeout
        gate_holder = self._sem.acquire(gate, max_holders=1, timeout_ms=timeout_ms, ttl_ms=ttl_ms)
        if gate_holder is None:
            logger.debug("Exclusive lock timeout (gate): %s", key)
            return None

        # Step 2: wait for existing readers to drain (sync poll)
        while True:
            info = self._sem.info(readers)
            if info is None or info.get("active_count", 0) == 0:
                break
            if time.monotonic() >= deadline:
                self._sem.release(gate, gate_holder)
                logger.debug("Exclusive lock timeout (readers drain): %s", key)
                return None
            time.sleep(self.RETRY_INTERVAL)

        self._active_locks[lock_id] = (self._SEM_LOCK, (gate, gate_holder))
        from nexus.lib.lock_order import L2_ADVISORY, mark_acquired

        mark_acquired(L2_ADVISORY)
        logger.debug("Exclusive lock acquired (gate pattern): %s -> %s", key, lock_id)
        return lock_id

    def _acquire_shared(
        self,
        key: str,
        lock_id: str,
        timeout: float,
        ttl_ms: int,
    ) -> str | None:
        """Reader: hold gate briefly to register, then hold readers slot."""
        gate = f"{key}:gate"
        readers = f"{key}:readers"
        timeout_ms = max(0, int(timeout * 1000))

        # Step 1: acquire gate with blocking timeout (blocks if writer holds it)
        gate_holder = self._sem.acquire(gate, max_holders=1, timeout_ms=timeout_ms, ttl_ms=ttl_ms)
        if gate_holder is None:
            logger.debug("Shared lock timeout (gate): %s", key)
            return None

        # Step 2: acquire readers slot (should never fail — 1024 slots)
        reader_holder = self._sem.acquire(
            readers, max_holders=_SHARED_MAX_HOLDERS, timeout_ms=0, ttl_ms=ttl_ms
        )

        # Step 3: release gate immediately — let other readers/writers through
        self._sem.release(gate, gate_holder)

        if reader_holder is None:
            logger.debug("Shared lock failed (reader slot): %s", key)
            return None

        self._active_locks[lock_id] = (self._SEM_LOCK, (readers, reader_holder))
        from nexus.lib.lock_order import L2_ADVISORY, mark_acquired

        mark_acquired(L2_ADVISORY)
        logger.debug("Shared lock acquired (gate pattern): %s -> %s", key, lock_id)
        return lock_id

    # -- release / extend / info ----------------------------------------------

    def release(self, lock_id: str, path: str) -> bool:  # noqa: ARG002 (path unused — lookup by lock_id)
        entry = self._active_locks.pop(lock_id, None)
        if entry is None:
            return False
        lock_type, payload = entry
        if lock_type == self._VFS_LOCK:
            _key, handle = payload
            released: bool = self._vfs_lock.release(handle)
        else:
            sem_name, holder_id = payload
            released = self._sem.release(sem_name, holder_id)
        if released:
            from nexus.lib.lock_order import L2_ADVISORY, mark_released

            mark_released(L2_ADVISORY)
            logger.debug("Lock released: %s (type=%s)", lock_id, lock_type)
        return released

    def extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
    ) -> ExtendResult:
        entry = self._active_locks.get(lock_id)
        if entry is None:
            return ExtendResult(success=False)
        lock_type, payload = entry
        if lock_type == self._VFS_LOCK:
            # lock_fast doesn't support TTL extension — treat as success (lock held in memory)
            lock_info = self.get_lock_info(path)
            return ExtendResult(success=True, lock_info=lock_info)
        sem_name, holder_id = payload
        ttl_ms = max(1000, int(ttl * 1000))
        success = self._sem.extend(sem_name, holder_id, ttl_ms=ttl_ms)
        if not success:
            return ExtendResult(success=False)
        lock_info = self.get_lock_info(path)
        return ExtendResult(success=True, lock_info=lock_info)

    def get_lock_info(self, path: str) -> LockInfo | None:
        key = self._lock_key(path)
        # Collect holders from _active_locks that belong to this path.
        holders: list[HolderInfo] = []
        for lid, (lock_type, payload) in self._active_locks.items():
            if lock_type == self._VFS_LOCK:
                vfs_key, _handle = payload
                if vfs_key == key:
                    holders.append(
                        HolderInfo(lock_id=lid, holder_info="", acquired_at=0.0, expires_at=0.0)
                    )
                continue
            sem_name, _hid = payload
            if sem_name == key or sem_name.startswith(f"{key}:"):
                holders.append(
                    HolderInfo(lock_id=lid, holder_info="", acquired_at=0.0, expires_at=0.0)
                )
        if not holders:
            return None
        max_h = len(holders)
        return LockInfo(
            path=path,
            mode="mutex" if max_h <= 1 else "semaphore",
            max_holders=max_h,
            holders=holders,
            fence_token=0,
        )

    def is_locked(self, path: str) -> bool:
        key = self._lock_key(path)
        for lock_type, payload in self._active_locks.values():
            if lock_type == self._VFS_LOCK:
                vfs_key, _handle = payload
                if vfs_key == key:
                    return True
                continue
            sem_name, _ = payload
            if sem_name == key or sem_name.startswith(f"{key}:"):
                return True
        return False

    def list_locks(self, pattern: str = "", limit: int = 100) -> list[LockInfo]:
        results: list[LockInfo] = []
        seen_paths: set[str] = set()
        for _lid, (lock_type, payload) in self._active_locks.items():
            if lock_type == self._VFS_LOCK:
                vfs_key, _handle = payload
                path = vfs_key
                if path in seen_paths:
                    continue
                if pattern and pattern not in path:
                    continue
                seen_paths.add(path)
                info = self.get_lock_info(path)
                if info is not None:
                    results.append(info)
                    if len(results) >= limit:
                        return results
                continue
            sem_name, _hid = payload
            # Strip suffixes (:gate, :readers) to get the base key
            base = sem_name
            for suffix in (":gate", ":readers"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            path = base
            if path in seen_paths:
                continue
            if pattern and pattern not in path:
                continue
            seen_paths.add(path)
            info = self.get_lock_info(path)
            if info is not None:
                results.append(info)
            if len(results) >= limit:
                break
        return results

    def force_release(self, path: str) -> bool:
        key = self._lock_key(path)
        released = False
        to_remove: list[str] = []
        for lid, (lock_type, payload) in self._active_locks.items():
            if lock_type == self._VFS_LOCK:
                vfs_key, handle = payload
                if vfs_key == key:
                    self._vfs_lock.release(handle)
                    to_remove.append(lid)
                    released = True
                continue
            sem_name, hid = payload
            if sem_name == key or sem_name.startswith(f"{key}:"):
                self._sem.release(sem_name, hid)
                to_remove.append(lid)
                released = True
        for lid in to_remove:
            del self._active_locks[lid]
        # Also force-release the underlying semaphores
        self._sem.force_release(key)
        self._sem.force_release(f"{key}:gate")
        self._sem.force_release(f"{key}:readers")
        if released:
            logger.debug("Lock force-released: %s", key)
        return released

    def health_check(self) -> bool:
        return True  # VFSSemaphore is always healthy (in-process)


# =============================================================================
# Backward compatibility aliases
# =============================================================================

LockManagerBase = AdvisoryLockManager
SemaphoreAdvisoryLockManager = LocalLockManager
