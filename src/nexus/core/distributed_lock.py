"""Distributed lock manager interface and Raft implementation.

This module provides the lock manager abstraction and Raft-based implementation
for coordinating access to resources across multiple Nexus nodes.

Architecture:
- LockManagerProtocol: Abstract interface for lock manager
- LockManagerBase: Abstract base class with common functionality
- RaftLockManager: Raft consensus-based locks (SSOT for strong consistency)

Lock Implementation:
- Uses Raft consensus via RaftMetadataStore for strong consistency
- Lock key: "{zone_id}:{path}" (resource to lock)
- Lock value: holder_id (UUID) for ownership verification
- TTL-based auto-expiry prevents deadlocks from crashed clients
- Supports both mutex (max_holders=1) and semaphore (max_holders>1)
- Advisory fencing tokens for stale-lock protection

Note: Redis-based locks have been removed. Raft is the only SSOT for locks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

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
    holders: list[HolderInfo]
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
# Abstract Interface (Protocol)
# =============================================================================


@runtime_checkable
class LockManagerProtocol(Protocol):
    """Protocol defining the lock manager interface.

    This protocol allows different backend implementations:
    - Raft consensus locks (default, implemented as RaftLockManager)

    All implementations must provide these async methods.
    """

    async def acquire(
        self,
        zone_id: str,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot.

        Args:
            zone_id: Zone ID for the lock
            path: Path to lock
            timeout: Maximum time to wait for lock
            ttl: Lock TTL (auto-expires after this)
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID if acquired, None on timeout
        """
        ...

    async def release(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock.

        Args:
            lock_id: Lock ID from acquire()
            zone_id: Zone ID
            path: Path that was locked

        Returns:
            True if released, False if not owned or expired
        """
        ...

    async def extend(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
        ttl: float = 30.0,
    ) -> ExtendResult:
        """Extend lock TTL (heartbeat).

        Args:
            lock_id: Lock ID from acquire()
            zone_id: Zone ID
            path: Path that was locked
            ttl: New TTL in seconds

        Returns:
            ExtendResult with success flag and updated lock info
        """
        ...

    async def get_lock_info(
        self,
        zone_id: str,
        path: str,
    ) -> LockInfo | None:
        """Get information about a lock.

        Args:
            zone_id: Zone ID
            path: Resource path

        Returns:
            LockInfo if locked, None if not locked
        """
        ...

    async def is_locked(self, zone_id: str, path: str) -> bool:
        """Check if a path is currently locked."""
        ...

    async def list_locks(
        self,
        zone_id: str,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks for a zone.

        Args:
            zone_id: Zone ID to list locks for
            pattern: Optional path filter
            limit: Maximum number of results

        Returns:
            List of LockInfo for active locks
        """
        ...

    async def force_release(
        self,
        zone_id: str,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation).

        Args:
            zone_id: Zone ID
            path: Resource path to force-release

        Returns:
            True if a lock was found and released, False if no lock exists
        """
        ...

    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        ...


class LockManagerBase(ABC):
    """Abstract base class for lock manager implementations.

    Provides common functionality and enforces the interface contract.
    Subclasses must implement all abstract methods.
    """

    DEFAULT_TTL = 30.0  # Default lock TTL in seconds
    DEFAULT_TIMEOUT = 30.0  # Default acquisition timeout

    @abstractmethod
    async def acquire(
        self,
        zone_id: str,
        path: str,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot.

        Args:
            zone_id: Zone ID for the lock
            path: Path to lock
            timeout: Maximum time to wait
            ttl: Lock TTL (auto-expires)
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID if acquired, None on timeout
        """
        pass

    @abstractmethod
    async def release(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock."""
        pass

    @abstractmethod
    async def extend(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
        ttl: float = DEFAULT_TTL,
    ) -> ExtendResult:
        """Extend lock TTL (heartbeat)."""
        pass

    @abstractmethod
    async def get_lock_info(
        self,
        zone_id: str,
        path: str,
    ) -> LockInfo | None:
        """Get information about a lock."""
        pass

    async def is_locked(self, zone_id: str, path: str) -> bool:
        """Check if a path is currently locked. Override for efficiency."""
        info = await self.get_lock_info(zone_id, path)
        return info is not None

    @abstractmethod
    async def list_locks(
        self,
        zone_id: str,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks for a zone."""
        pass

    @abstractmethod
    async def force_release(
        self,
        zone_id: str,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation)."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        pass


# =============================================================================
# Raft Implementation
# =============================================================================

# Monotonically increasing fence token counter (per-process)
_fence_token_counter: int = 0


def _next_fence_token() -> int:
    """Generate the next monotonically increasing fence token."""
    global _fence_token_counter
    _fence_token_counter += 1
    return _fence_token_counter


class RaftLockManager(LockManagerBase):
    """Raft-based distributed locking with strong consistency.

    Uses Raft consensus via RaftMetadataStore for CP (strong consistency) locks.
    This is the SSOT for all distributed locks in Nexus.

    Features:
    - Strong consistency via Raft consensus
    - Atomic acquisition with retry loop
    - Ownership verification on release/extend
    - TTL-based auto-expiry (default: 30s)
    - Supports mutex (max_holders=1) and semaphore (max_holders>1)
    - Advisory fencing tokens for stale-lock protection

    Example:
        >>> from nexus.storage.raft_metadata_store import RaftMetadataStore
        >>> store = RaftMetadataStore.local("/var/lib/nexus/metadata")
        >>> manager = RaftLockManager(store)
        >>> lock_id = await manager.acquire("default", "/file.txt", timeout=5.0)
        >>> if lock_id:
        ...     try:
        ...         # Do exclusive work...
        ...         pass
        ...     finally:
        ...         await manager.release(lock_id, "default", "/file.txt")
    """

    # Retry parameters for acquisition
    RETRY_BASE_INTERVAL = 0.05  # Start with 50ms
    RETRY_MAX_INTERVAL = 1.0  # Cap at 1 second
    RETRY_MULTIPLIER = 2.0  # Double each retry

    def __init__(self, raft_store: RaftMetadataStore):
        """Initialize RaftLockManager.

        Args:
            raft_store: RaftMetadataStore instance for lock storage
        """
        self._store = raft_store

    def _lock_key(self, zone_id: str, path: str) -> str:
        """Get the lock key combining zone and path."""
        return f"{zone_id}:{path}"

    def _parse_lock_key(self, lock_key: str) -> tuple[str, str]:
        """Parse a lock key into (zone_id, path)."""
        zone_id, _, path = lock_key.partition(":")
        return zone_id, path

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
            fence_token=_next_fence_token(),
        )

    async def acquire(
        self,
        zone_id: str,
        path: str,
        timeout: float = LockManagerBase.DEFAULT_TIMEOUT,
        ttl: float = LockManagerBase.DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock using Raft consensus.

        Args:
            zone_id: Zone ID for the lock
            path: Path to lock (e.g., "/shared/config.json")
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds - auto-expires after this
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID (string) if acquired, None on timeout
        """
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        lock_key = self._lock_key(zone_id, path)
        holder_id = str(uuid.uuid4())
        ttl_secs = int(ttl)

        deadline = asyncio.get_event_loop().time() + timeout
        retry_interval = self.RETRY_BASE_INTERVAL

        while True:
            # Try to acquire lock via Raft
            acquired = self._store.acquire_lock(
                lock_key, holder_id, max_holders=max_holders, ttl_secs=ttl_secs
            )

            if acquired:
                logger.debug(
                    f"Raft lock acquired: {lock_key} -> {holder_id} "
                    f"(max_holders={max_holders}, TTL={ttl}s)"
                )
                return holder_id

            # Check if we've exceeded timeout
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.debug(f"Raft lock acquisition timeout: {lock_key}")
                return None

            # Exponential backoff
            sleep_time = min(retry_interval, remaining)
            await asyncio.sleep(sleep_time)

            # Increase interval for next retry
            retry_interval = min(
                retry_interval * self.RETRY_MULTIPLIER,
                self.RETRY_MAX_INTERVAL,
            )

    async def release(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock.

        Only releases if caller owns the lock (lock_id matches holder_id).

        Args:
            lock_id: Lock ID from acquire() (this is the holder_id)
            zone_id: Zone ID
            path: Path that was locked

        Returns:
            True if released, False if not owned or expired
        """
        lock_key = self._lock_key(zone_id, path)

        try:
            released = self._store.release_lock(lock_key, lock_id)
            if released:
                logger.debug(f"Raft lock released: {lock_key}")
            else:
                logger.debug(f"Raft lock release failed (not owned or expired): {lock_key}")
            return released
        except Exception as e:
            logger.error(f"Failed to release Raft lock {lock_key}: {e}")
            return False

    async def extend(
        self,
        lock_id: str,
        zone_id: str,
        path: str,
        ttl: float = LockManagerBase.DEFAULT_TTL,
    ) -> ExtendResult:
        """Extend a lock's TTL (heartbeat).

        Only succeeds if the caller currently holds the lock (ownership verified).
        Returns full lock info to avoid extra roundtrips.

        Args:
            lock_id: Lock ID from acquire()
            zone_id: Zone ID
            path: Path that was locked
            ttl: New TTL in seconds

        Returns:
            ExtendResult with success flag and updated lock info
        """
        lock_key = self._lock_key(zone_id, path)
        ttl_secs = int(ttl)

        try:
            extended = self._store.extend_lock(lock_key, lock_id, ttl_secs)
            if not extended:
                logger.debug(f"Raft lock extend failed (not owned or expired): {lock_key}")
                return ExtendResult(success=False)

            logger.debug(f"Raft lock extended: {lock_key} (new TTL: {ttl}s)")
            # Fetch updated lock info to return with the result
            lock_info = await self.get_lock_info(zone_id, path)
            return ExtendResult(success=True, lock_info=lock_info)
        except Exception as e:
            logger.error(f"Failed to extend Raft lock {lock_key}: {e}")
            return ExtendResult(success=False)

    async def get_lock_info(self, zone_id: str, path: str) -> LockInfo | None:
        """Get information about a lock.

        Args:
            zone_id: Zone ID
            path: Resource path

        Returns:
            LockInfo if locked with active holders, None if not locked
        """
        lock_key = self._lock_key(zone_id, path)

        try:
            store_info = self._store.get_lock_info(lock_key)
            if store_info is None:
                return None
            return self._store_info_to_lock_info(store_info)
        except Exception as e:
            logger.error(f"Failed to get lock info for {lock_key}: {e}")
            return None

    async def is_locked(self, zone_id: str, path: str) -> bool:
        """Check if a path is currently locked."""
        info = await self.get_lock_info(zone_id, path)
        return info is not None

    async def list_locks(
        self,
        zone_id: str,
        pattern: str = "",
        limit: int = 100,
    ) -> list[LockInfo]:
        """List active locks for a zone.

        Args:
            zone_id: Zone ID to list locks for
            pattern: Optional path filter (unused for now, reserved)
            limit: Maximum number of results

        Returns:
            List of LockInfo for active locks in this zone
        """
        prefix = f"{zone_id}:"

        try:
            store_locks = self._store.list_locks(prefix=prefix, limit=limit)
            return [self._store_info_to_lock_info(info) for info in store_locks]
        except Exception as e:
            logger.error(f"Failed to list locks for zone {zone_id}: {e}")
            return []

    async def force_release(
        self,
        zone_id: str,
        path: str,
    ) -> bool:
        """Force-release all holders of a lock (admin operation).

        Args:
            zone_id: Zone ID
            path: Resource path to force-release

        Returns:
            True if a lock was found and released, False if no lock exists
        """
        lock_key = self._lock_key(zone_id, path)

        try:
            released = self._store.force_release_lock(lock_key)
            if released:
                logger.warning(f"Raft lock force-released: {lock_key}")
            else:
                logger.debug(f"Raft lock force-release: no lock found for {lock_key}")
            return released
        except Exception as e:
            logger.error(f"Failed to force-release Raft lock {lock_key}: {e}")
            return False

    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        try:
            # Simple health check: try to get metadata (tests store is operational)
            self._store.get("/__health_check__")
            return True
        except Exception as e:
            logger.warning(f"Raft lock manager health check failed: {e}")
            return False


# Backward compatibility alias (points to Raft now)
DistributedLockManager = RaftLockManager


# =============================================================================
# Factory and Singleton Management
# =============================================================================


def create_lock_manager(
    raft_store: RaftMetadataStore | None = None,
    **kwargs: Any,  # noqa: ARG001 - Reserved for future use
) -> LockManagerBase:
    """Factory function to create a lock manager instance.

    Args:
        raft_store: RaftMetadataStore for lock storage
        **kwargs: Reserved for future use

    Returns:
        LockManagerBase implementation (RaftLockManager)

    Raises:
        ValueError: If raft_store is not provided
    """
    if raft_store is None:
        raise ValueError("raft_store is required")
    return RaftLockManager(raft_store)


# Singleton instance for shared use
_distributed_lock_manager: LockManagerBase | None = None


def get_distributed_lock_manager() -> LockManagerBase | None:
    """Get the global distributed lock manager instance.

    Returns:
        LockManagerBase instance if initialized, None otherwise
    """
    return _distributed_lock_manager


def set_distributed_lock_manager(manager: LockManagerBase | None) -> None:
    """Set the global distributed lock manager instance.

    Args:
        manager: LockManagerBase instance to set as global, or None to clear
    """
    global _distributed_lock_manager
    _distributed_lock_manager = manager
