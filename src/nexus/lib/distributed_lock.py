"""Advisory lock manager — ABCs and LocalLockManager.

Advisory locks are *metadata* — visible, queryable, TTL-based.
Used for user/service coordination (task queues, turn-taking, resource
contention).  Distinct from kernel I/O locks (VFSLockManager, ~200ns,
in-memory, process-scoped).

Architecture:
- LockStoreProtocol: Low-level store interface (MetastoreABC lock methods)
- LockManagerBase: Async advisory lock API (zone_id bound at construction)
- LocalLockManager: Standalone mode — wraps MetastoreABC (this file)
- RaftLockManager: Federation mode — wraps RaftMetadataStore (raft/)

Factory.py injects LocalLockManager (standalone) or RaftLockManager
(federation).  Callers see only LockManagerBase.

References:
    - docs/architecture/lock-architecture.md
    - docs/architecture/federation-memo.md §6.9
"""

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

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


class LockManagerBase(ABC):
    """Abstract base class for advisory lock manager implementations.

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
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire an advisory lock or semaphore slot.

        Args:
            path: Path to lock
            timeout: Maximum time to wait
            ttl: Lock TTL (auto-expires)
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID if acquired, None on timeout
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
# Concrete: LocalLockManager (standalone mode)
# =============================================================================


class LocalLockManager(LockManagerBase):
    """Advisory lock manager for standalone mode (no Raft).

    Wraps MetastoreABC's lock methods with async interface + retry.
    Same LockManagerBase API as RaftLockManager — callers don't know
    the difference.  Factory.py injects this when Raft is not enabled.

    Differences from RaftLockManager:
    - Fixed retry interval (50ms) instead of exponential backoff
      (local redb is ~5μs, no network jitter to absorb)
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
        timeout: float = LockManagerBase.DEFAULT_TIMEOUT,
        ttl: float = LockManagerBase.DEFAULT_TTL,
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
        ttl: float = LockManagerBase.DEFAULT_TTL,
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
