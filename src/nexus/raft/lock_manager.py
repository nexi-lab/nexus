"""Raft-based distributed lock manager (federation-memo §6.9).

Concrete ``RaftLockManager`` implementation using Raft consensus via
any ``LockStoreProtocol``-compatible store for strong-consistency distributed locks.

Moved from ``nexus.core.distributed_lock`` — the ABCs/Protocols remain
in core, but the Raft-specific concrete class belongs in the raft module.

References:
    - docs/architecture/federation-memo.md §6.9
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
"""

import asyncio
import logging
import uuid
from typing import Any

from nexus.lib.distributed_lock import (
    ExtendResult,
    HolderInfo,
    LockInfo,
    LockManagerBase,
    LockStoreProtocol,
)

logger = logging.getLogger(__name__)


class RaftLockManager(LockManagerBase):
    """Raft-based distributed locking with strong consistency.

    Uses any LockStoreProtocol-compatible store (e.g., RaftMetadataStore)
    for CP (strong consistency) locks.
    This is the SSOT for all distributed locks in Nexus.

    Features:
    - Strong consistency via Raft consensus
    - Atomic acquisition with retry loop
    - Ownership verification on release/extend
    - TTL-based auto-expiry (default: 30s)
    - Supports mutex (max_holders=1) and semaphore (max_holders>1)

    Note:
        Fence tokens are set to 0 (not yet provided by the store layer).
        When the Rust engine exposes per-lock monotonic counters (e.g. from
        the Raft log index), ``_store_info_to_lock_info`` should read
        ``store_info["fence_token"]`` instead.

    Example:
        >>> from nexus.storage.raft_metadata_store import RaftMetadataStore
        >>> store = RaftMetadataStore.embedded("/var/lib/nexus/metadata")
        >>> manager = RaftLockManager(store)  # RaftMetadataStore satisfies LockStoreProtocol
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

    def __init__(self, raft_store: LockStoreProtocol) -> None:
        """Initialize RaftLockManager.

        Args:
            raft_store: LockStoreProtocol instance for lock storage
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
            fence_token=store_info.get("fence_token", 0),
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

        deadline = asyncio.get_running_loop().time() + timeout
        retry_interval = self.RETRY_BASE_INTERVAL

        while True:
            # Try to acquire lock via Raft
            acquired = self._store.acquire_lock(
                lock_key, holder_id, max_holders=max_holders, ttl_secs=ttl_secs
            )

            if acquired:
                logger.debug(
                    "Raft lock acquired: %s -> %s (max_holders=%d, TTL=%ss)",
                    lock_key,
                    holder_id,
                    max_holders,
                    ttl,
                )
                return holder_id

            # Check if we've exceeded timeout
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.debug("Raft lock acquisition timeout: %s", lock_key)
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
                logger.debug("Raft lock released: %s", lock_key)
            else:
                logger.debug("Raft lock release failed (not owned or expired): %s", lock_key)
            return released
        except Exception as e:
            logger.error("Failed to release Raft lock %s: %s", lock_key, e)
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
                logger.debug("Raft lock extend failed (not owned or expired): %s", lock_key)
                return ExtendResult(success=False)

            logger.debug("Raft lock extended: %s (new TTL: %ss)", lock_key, ttl)
            # Fetch updated lock info to return with the result
            lock_info = await self.get_lock_info(zone_id, path)
            return ExtendResult(success=True, lock_info=lock_info)
        except Exception as e:
            logger.error("Failed to extend Raft lock %s: %s", lock_key, e)
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
            logger.error("Failed to get lock info for %s: %s", lock_key, e)
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
            if store_locks is None:
                return []
            results = [self._store_info_to_lock_info(info) for info in store_locks]
            if pattern:
                results = [r for r in results if pattern in r.path]
            return results
        except Exception as e:
            logger.error("Failed to list locks for zone %s: %s", zone_id, e)
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
                logger.warning("Raft lock force-released: %s", lock_key)
            else:
                logger.debug("Raft lock force-release: no lock found for %s", lock_key)
            return released
        except Exception as e:
            logger.error("Failed to force-release Raft lock %s: %s", lock_key, e)
            return False

    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        try:
            # Simple health check: try to get metadata (tests store is operational)
            self._store.get("/__health_check__")
            return True
        except Exception as e:
            logger.warning("Raft lock manager health check failed: %s", e)
            return False


# =============================================================================
# Factory and Singleton Management
# =============================================================================


def create_lock_manager(
    raft_store: LockStoreProtocol | None = None,
    **kwargs: Any,  # noqa: ARG001 - Reserved for future use
) -> LockManagerBase:
    """Factory function to create a lock manager instance.

    Args:
        raft_store: LockStoreProtocol for lock storage
        **kwargs: Reserved for future use

    Returns:
        LockManagerBase implementation (RaftLockManager)

    Raises:
        ValueError: If raft_store is not provided
    """
    if raft_store is None:
        raise ValueError("raft_store is required")
    return RaftLockManager(raft_store)
