"""Advisory lock manager — ABCs and SemaphoreAdvisoryLockManager.

Advisory locks are *metadata* — visible, queryable, TTL-based.
Used for user/service coordination (task queues, turn-taking, resource
contention).  Distinct from kernel I/O locks (VFSLockManager, ~200ns,
in-memory, process-scoped).

Architecture:
- LockStoreProtocol: Low-level store interface (MetastoreABC lock methods)
- AdvisoryLockManager: Async advisory lock API (POSIX flock(2), zone_id bound at construction)
- SemaphoreAdvisoryLockManager: Standalone mode — wraps VFSSemaphore (this file)
- RaftLockManager: Federation mode — wraps RaftMetadataStore (raft/)

Factory.py injects SemaphoreAdvisoryLockManager (standalone) or RaftLockManager
(federation).  Callers see only AdvisoryLockManager.

References:
    - docs/architecture/lock-architecture.md
    - docs/architecture/federation-memo.md §6.9
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.semaphore import VFSSemaphoreProtocol

logger = logging.getLogger(__name__)

# =============================================================================
# Low-level Store Protocol
# =============================================================================


@runtime_checkable
class LockStoreProtocol(Protocol):
    """Protocol for lock-capable metadata stores (e.g., RaftMetadataStore).

    Captures the interface that lock managers need, decoupling
    the lock manager from the concrete storage driver
    (KERNEL-ARCHITECTURE.md §1).
    """

    def acquire_lock(
        self,
        lock_key: str,
        holder_id: str,
        *,
        max_holders: int = 1,
        ttl_secs: int = 30,
    ) -> bool:
        """Atomically acquire a lock."""
        ...

    def release_lock(self, lock_key: str, holder_id: str) -> bool:
        """Release a lock held by holder_id."""
        ...

    def extend_lock(self, lock_key: str, holder_id: str, ttl_secs: int) -> bool:
        """Extend a lock's TTL."""
        ...

    def get_lock_info(self, lock_key: str) -> dict[str, Any] | None:
        """Get information about a lock."""
        ...

    def list_locks(self, *, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """List active locks matching prefix."""
        ...

    def force_release_lock(self, lock_key: str) -> bool:
        """Force-release all holders of a lock."""
        ...

    def get(self, key: str) -> Any:
        """Get a value by key (used for health checks)."""
        ...


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

    zone_id is bound at construction time — callers never pass it per-method.
    This prevents federation concepts (zone) from leaking into the lock API.
    Internally, zone_id is used as a key prefix for store-level scoping.

    Subclasses must implement all abstract methods.
    """

    DEFAULT_TTL = 30.0  # Default lock TTL in seconds
    DEFAULT_TIMEOUT = 30.0  # Default acquisition timeout

    def __init__(self, *, zone_id: str = "root") -> None:
        self._zone_id = zone_id

    def _lock_key(self, path: str) -> str:
        """Compose store-level lock key from zone_id + path."""
        return f"{self._zone_id}:{path}"

    def _parse_lock_key(self, lock_key: str) -> tuple[str, str]:
        """Parse a lock key into (zone_id, path)."""
        zone_id, _, path = lock_key.partition(":")
        return zone_id, path

    @abstractmethod
    async def acquire(
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
    async def release(
        self,
        lock_id: str,
        path: str,
    ) -> bool:
        """Release an advisory lock."""

    @abstractmethod
    async def extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = DEFAULT_TTL,
    ) -> ExtendResult:
        """Extend lock TTL (heartbeat)."""

    @abstractmethod
    async def get_lock_info(
        self,
        path: str,
    ) -> LockInfo | None:
        """Get information about a lock."""

    async def is_locked(self, path: str) -> bool:
        """Check if a path is currently locked. Override for efficiency."""
        info = await self.get_lock_info(path)
        return info is not None

    @abstractmethod
    async def list_locks(
        self,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks."""

    @abstractmethod
    async def force_release(
        self,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation)."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""


# =============================================================================
# Concrete: SemaphoreAdvisoryLockManager (standalone mode)
# =============================================================================

_SHARED_MAX_HOLDERS = 1024  # max concurrent shared (reader) holders


class SemaphoreAdvisoryLockManager(AdvisoryLockManager):
    """Advisory lock manager for standalone mode — wraps VFSSemaphore.

    Replaces ``LocalLockManager``.  Uses kernel ``VFSSemaphore`` primitive
    directly instead of going through MetastoreABC lock methods.

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

    RW gate pattern (shared/exclusive)::

        Two semaphores per path:
          "{key}:gate"    — max_holders=1, writer holds during entire write
          "{key}:readers" — max_holders=1024, each reader takes one slot

        Writer (exclusive):
          1. acquire gate (blocks new readers from entering)
          2. poll-wait for readers semaphore to drain (active_count == 0)
          3. ... critical section ...
          4. release gate

        Reader (shared):
          1. acquire gate (brief — just to check no writer is active)
          2. acquire readers slot
          3. release gate (let other readers through)
          4. ... critical section ...
          5. release readers slot

    Differences from ``LocalLockManager``:
    - No MetastoreABC dependency — uses VFSSemaphore directly
    - Proper shared/exclusive semantics via two-semaphore gate pattern
    - Lock state tracked in ``_active_locks`` dict (process-local)
    """

    RETRY_INTERVAL = 0.05  # 50ms between retries

    def __init__(
        self,
        semaphore: VFSSemaphoreProtocol,
        *,
        zone_id: str = "root",
    ) -> None:
        super().__init__(zone_id=zone_id)
        self._sem = semaphore
        # lock_id → (semaphore_name, holder_id) for release/extend
        self._active_locks: dict[str, tuple[str, str]] = {}

    # -- acquire modes --------------------------------------------------------

    async def acquire(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",
        timeout: float = AdvisoryLockManager.DEFAULT_TIMEOUT,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        key = self._lock_key(path)
        ttl_ms = max(1000, int(ttl * 1000))
        lock_id = str(uuid.uuid4())

        if max_holders > 1:
            # Counting semaphore — async retry loop (VFSSemaphore.acquire blocks sync)
            holder = self._sem.acquire(key, max_holders=max_holders, timeout_ms=0, ttl_ms=ttl_ms)
            if holder is not None:
                self._active_locks[lock_id] = (key, holder)
                logger.debug(
                    "Counting lock acquired: %s -> %s (max_holders=%d)", key, lock_id, max_holders
                )
                return lock_id
            if timeout <= 0:
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(self.RETRY_INTERVAL)
                holder = self._sem.acquire(
                    key, max_holders=max_holders, timeout_ms=0, ttl_ms=ttl_ms
                )
                if holder is not None:
                    self._active_locks[lock_id] = (key, holder)
                    return lock_id
            return None

        if mode == "exclusive":
            return await self._acquire_exclusive(key, lock_id, timeout, ttl_ms)
        else:  # shared
            return await self._acquire_shared(key, lock_id, timeout, ttl_ms)

    async def _acquire_exclusive(
        self,
        key: str,
        lock_id: str,
        timeout: float,
        ttl_ms: int,
    ) -> str | None:
        """Writer: hold gate for duration, wait for readers to drain.

        Uses timeout_ms=0 on VFSSemaphore + async retry to avoid blocking
        the event loop (VFSSemaphore.acquire with timeout_ms>0 blocks sync).
        """
        gate = f"{key}:gate"
        readers = f"{key}:readers"
        deadline = time.monotonic() + timeout

        # Step 1: acquire gate with async retry
        gate_holder = self._sem.acquire(gate, max_holders=1, timeout_ms=0, ttl_ms=ttl_ms)
        while gate_holder is None:
            if time.monotonic() >= deadline:
                logger.debug("Exclusive lock timeout (gate): %s", key)
                return None
            await asyncio.sleep(self.RETRY_INTERVAL)
            gate_holder = self._sem.acquire(gate, max_holders=1, timeout_ms=0, ttl_ms=ttl_ms)

        # Step 2: wait for existing readers to drain
        while True:
            info = self._sem.info(readers)
            if info is None or info.get("active_count", 0) == 0:
                break
            if time.monotonic() >= deadline:
                self._sem.release(gate, gate_holder)
                logger.debug("Exclusive lock timeout (readers drain): %s", key)
                return None
            await asyncio.sleep(self.RETRY_INTERVAL)

        self._active_locks[lock_id] = (gate, gate_holder)
        logger.debug("Exclusive lock acquired: %s -> %s", key, lock_id)
        return lock_id

    async def _acquire_shared(
        self,
        key: str,
        lock_id: str,
        timeout: float,
        ttl_ms: int,
    ) -> str | None:
        """Reader: hold gate briefly to register, then hold readers slot."""
        gate = f"{key}:gate"
        readers = f"{key}:readers"
        deadline = time.monotonic() + timeout

        # Step 1: acquire gate with async retry (blocks if writer holds it)
        gate_holder = self._sem.acquire(gate, max_holders=1, timeout_ms=0, ttl_ms=ttl_ms)
        while gate_holder is None:
            if time.monotonic() >= deadline:
                logger.debug("Shared lock timeout (gate): %s", key)
                return None
            await asyncio.sleep(self.RETRY_INTERVAL)
            gate_holder = self._sem.acquire(gate, max_holders=1, timeout_ms=0, ttl_ms=ttl_ms)

        # Step 2: acquire readers slot (should never fail — 1024 slots)
        reader_holder = self._sem.acquire(
            readers, max_holders=_SHARED_MAX_HOLDERS, timeout_ms=0, ttl_ms=ttl_ms
        )

        # Step 3: release gate immediately — let other readers/writers through
        self._sem.release(gate, gate_holder)

        if reader_holder is None:
            logger.debug("Shared lock failed (reader slot): %s", key)
            return None

        self._active_locks[lock_id] = (readers, reader_holder)
        logger.debug("Shared lock acquired: %s -> %s", key, lock_id)
        return lock_id

    # -- release / extend / info ----------------------------------------------

    async def release(self, lock_id: str, path: str) -> bool:  # noqa: ARG002 (path unused — lookup by lock_id)
        entry = self._active_locks.pop(lock_id, None)
        if entry is None:
            return False
        sem_name, holder_id = entry
        released = self._sem.release(sem_name, holder_id)
        if released:
            logger.debug("Lock released: %s (sem=%s)", lock_id, sem_name)
        return released

    async def extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
    ) -> ExtendResult:
        entry = self._active_locks.get(lock_id)
        if entry is None:
            return ExtendResult(success=False)
        sem_name, holder_id = entry
        ttl_ms = max(1000, int(ttl * 1000))
        success = self._sem.extend(sem_name, holder_id, ttl_ms=ttl_ms)
        if not success:
            return ExtendResult(success=False)
        lock_info = await self.get_lock_info(path)
        return ExtendResult(success=True, lock_info=lock_info)

    async def get_lock_info(self, path: str) -> LockInfo | None:
        key = self._lock_key(path)
        # Collect holders from _active_locks that belong to this path.
        # A lock's semaphore name starts with key (plain key, key:gate, key:readers).
        holders: list[HolderInfo] = []
        for lid, (sem_name, _hid) in self._active_locks.items():
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

    async def is_locked(self, path: str) -> bool:
        key = self._lock_key(path)
        for sem_name, _ in self._active_locks.values():
            if sem_name == key or sem_name.startswith(f"{key}:"):
                return True
        return False

    async def list_locks(self, pattern: str = "", limit: int = 100) -> list[LockInfo]:
        results: list[LockInfo] = []
        seen_paths: set[str] = set()
        for _lid, (sem_name, _hid) in self._active_locks.items():
            # Strip suffixes (:gate, :readers) to get the base key
            base = sem_name
            for suffix in (":gate", ":readers"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            _, path = self._parse_lock_key(base)
            if path in seen_paths:
                continue
            if pattern and pattern not in path:
                continue
            seen_paths.add(path)
            info = await self.get_lock_info(path)
            if info is not None:
                results.append(info)
            if len(results) >= limit:
                break
        return results

    async def force_release(self, path: str) -> bool:
        key = self._lock_key(path)
        released = False
        to_remove: list[str] = []
        for lid, (sem_name, hid) in self._active_locks.items():
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

    async def health_check(self) -> bool:
        return True  # VFSSemaphore is always healthy (in-process)


# =============================================================================
# Legacy: LocalLockManager (kept for backward compat during migration)
# =============================================================================


class LocalLockManager(AdvisoryLockManager):
    """Advisory lock manager for standalone mode (no Raft).

    .. deprecated::
        Use ``SemaphoreAdvisoryLockManager`` instead.  ``LocalLockManager``
        wraps MetastoreABC lock methods; the new implementation wraps
        VFSSemaphore directly.  This class is retained during the migration
        period and will be removed once factory wiring is updated.

    Wraps MetastoreABC's lock methods with async interface + retry.
    Same AdvisoryLockManager API as RaftLockManager — callers don't know
    the difference.  Factory.py injects this when Raft is not enabled.

    Differences from RaftLockManager:
    - Fixed retry interval (50ms) instead of exponential backoff
      (local redb is ~5us, no network jitter to absorb)
    - Uses time.monotonic() instead of asyncio loop time
    - No Raft consensus overhead
    """

    RETRY_INTERVAL = 0.05  # 50ms between retries (local store is fast)

    def __init__(self, store: LockStoreProtocol, *, zone_id: str = "root") -> None:
        super().__init__(zone_id=zone_id)
        self._store = store

    def _store_info_to_lock_info(self, store_info: dict[str, Any]) -> LockInfo:
        """Convert store-level lock info dict to a LockInfo dataclass."""
        lock_key = store_info["path"]
        _, resource_path = self._parse_lock_key(lock_key)
        max_holders = store_info["max_holders"]
        holders = [
            HolderInfo(
                lock_id=h["lock_id"],
                holder_info=h.get("holder_info", ""),
                acquired_at=float(h.get("acquired_at", 0)),
                expires_at=float(h.get("expires_at", 0)),
            )
            for h in store_info.get("holders", [])
        ]
        return LockInfo(
            path=resource_path,
            mode="mutex" if max_holders == 1 else "semaphore",
            max_holders=max_holders,
            holders=holders,
            fence_token=store_info.get("fence_token", 0),
        )

    async def acquire(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",  # noqa: ARG002 (MetastoreABC has no shared mode)
        timeout: float = AdvisoryLockManager.DEFAULT_TIMEOUT,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        lock_key = self._lock_key(path)
        holder_id = str(uuid.uuid4())
        ttl_secs = max(1, int(ttl))

        # First attempt
        if self._store.acquire_lock(
            lock_key, holder_id, max_holders=max_holders, ttl_secs=ttl_secs
        ):
            logger.debug("Local lock acquired: %s -> %s", lock_key, holder_id)
            return holder_id

        if timeout <= 0:
            return None

        # Retry loop with fixed interval
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(min(self.RETRY_INTERVAL, deadline - time.monotonic()))
            if self._store.acquire_lock(
                lock_key, holder_id, max_holders=max_holders, ttl_secs=ttl_secs
            ):
                logger.debug("Local lock acquired: %s -> %s", lock_key, holder_id)
                return holder_id

        logger.debug("Local lock acquisition timeout: %s", lock_key)
        return None

    async def release(self, lock_id: str, path: str) -> bool:
        lock_key = self._lock_key(path)
        released = self._store.release_lock(lock_key, lock_id)
        if released:
            logger.debug("Local lock released: %s", lock_key)
        return released

    async def extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
    ) -> ExtendResult:
        lock_key = self._lock_key(path)
        ttl_secs = max(1, int(ttl))
        success = self._store.extend_lock(lock_key, lock_id, ttl_secs)
        if not success:
            return ExtendResult(success=False)
        lock_info = await self.get_lock_info(path)
        return ExtendResult(success=True, lock_info=lock_info)

    async def get_lock_info(self, path: str) -> LockInfo | None:
        lock_key = self._lock_key(path)
        store_info = self._store.get_lock_info(lock_key)
        if store_info is None:
            return None
        return self._store_info_to_lock_info(store_info)

    async def list_locks(self, pattern: str = "", limit: int = 100) -> list[LockInfo]:
        prefix = f"{self._zone_id}:"
        store_locks = self._store.list_locks(prefix=prefix, limit=limit)
        results = [self._store_info_to_lock_info(info) for info in store_locks]
        if pattern:
            results = [r for r in results if pattern in r.path]
        return results

    async def force_release(self, path: str) -> bool:
        lock_key = self._lock_key(path)
        released = self._store.force_release_lock(lock_key)
        if released:
            logger.debug("Local lock force-released: %s", lock_key)
        return released

    async def health_check(self) -> bool:
        return True  # local store is always healthy


# =============================================================================
# Backward compatibility aliases
# =============================================================================

LockManagerBase = AdvisoryLockManager
