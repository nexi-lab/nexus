"""Raft-based distributed lock manager (federation-memo §6.9).

Concrete ``RaftLockManager`` implementation using Raft consensus via
any ``LockStoreProtocol``-compatible store for strong-consistency distributed locks.

Architecture:
    LockManagerBase (lib/distributed_lock.py) defines the async advisory lock API.
    Lock keys are zone-canonical paths from PathRouter.
    RaftLockManager adds exponential backoff (network jitter) on top.

References:
    - docs/architecture/federation-memo.md §6.9
    - docs/architecture/lock-architecture.md
"""

import logging
import uuid
from typing import Any, Literal

from nexus.lib.distributed_lock import (
    AdvisoryLockManager,
    ExtendResult,
    HolderInfo,
    LockInfo,
    LockManagerBase,
)

logger = logging.getLogger(__name__)


class RaftLockManager(LockManagerBase):
    """Raft-based distributed locking with strong consistency.

    Uses any LockStoreProtocol-compatible store (e.g., RaftMetadataStore)
    for CP (strong consistency) locks.

    Features:
    - Strong consistency via Raft consensus
    - Atomic acquisition with exponential backoff retry
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
        >>> manager = RaftLockManager(store, zone_id="zone-1")
        >>> lock_id = manager.acquire("/file.txt", timeout=5.0)
        >>> if lock_id:
        ...     try:
        ...         # Do exclusive work...
        ...         pass
        ...     finally:
        ...         manager.release(lock_id, "/file.txt")
    """

    # Retry parameters for acquisition
    RETRY_BASE_INTERVAL = 0.05  # Start with 50ms
    RETRY_MAX_INTERVAL = 1.0  # Cap at 1 second
    RETRY_MULTIPLIER = 2.0  # Double each retry

    def __init__(self, raft_store: Any) -> None:
        """Initialize RaftLockManager.

        Args:
            raft_store: Lock-capable metadata store (e.g., RaftMetadataStore)
        """
        super().__init__()
        self._store = raft_store

    def _store_info_to_lock_info(self, store_info: dict[str, Any]) -> LockInfo:
        """Convert store-level lock info dict to a LockInfo dataclass."""
        resource_path = store_info["path"]
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

    def acquire(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",  # noqa: ARG002 (Raft store has no shared mode yet)
        timeout: float = AdvisoryLockManager.DEFAULT_TIMEOUT,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        from nexus.lib.lock_order import L2_ADVISORY, assert_can_acquire, mark_acquired

        assert_can_acquire(L2_ADVISORY)
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        lock_key = self._lock_key(path)
        holder_id = str(uuid.uuid4())
        ttl_secs = max(1, int(ttl))

        import time as _time

        deadline = _time.monotonic() + timeout
        retry_interval = self.RETRY_BASE_INTERVAL

        while True:
            acquired = self._store.acquire_lock(
                lock_key, holder_id, max_holders=max_holders, ttl_secs=ttl_secs
            )

            if acquired:
                mark_acquired(L2_ADVISORY)
                logger.debug(
                    "Raft lock acquired: %s -> %s (max_holders=%d, TTL=%ss)",
                    lock_key,
                    holder_id,
                    max_holders,
                    ttl,
                )
                return holder_id

            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                logger.debug("Raft lock acquisition timeout: %s", lock_key)
                return None

            sleep_time = min(retry_interval, remaining)
            _time.sleep(sleep_time)

            retry_interval = min(
                retry_interval * self.RETRY_MULTIPLIER,
                self.RETRY_MAX_INTERVAL,
            )

    def release(self, lock_id: str, path: str) -> bool:
        lock_key = self._lock_key(path)
        try:
            released: bool = self._store.release_lock(lock_key, lock_id)
            if released:
                from nexus.lib.lock_order import L2_ADVISORY, mark_released

                mark_released(L2_ADVISORY)
                logger.debug("Raft lock released: %s", lock_key)
            else:
                logger.debug("Raft lock release failed (not owned or expired): %s", lock_key)
            return released
        except Exception as e:
            logger.error("Failed to release Raft lock %s: %s", lock_key, e)
            return False

    def extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = AdvisoryLockManager.DEFAULT_TTL,
    ) -> ExtendResult:
        lock_key = self._lock_key(path)
        ttl_secs = max(1, int(ttl))
        try:
            extended = self._store.extend_lock(lock_key, lock_id, ttl_secs)
            if not extended:
                logger.debug("Raft lock extend failed (not owned or expired): %s", lock_key)
                return ExtendResult(success=False)

            logger.debug("Raft lock extended: %s (new TTL: %ss)", lock_key, ttl)
            lock_info = self.get_lock_info(path)
            return ExtendResult(success=True, lock_info=lock_info)
        except Exception as e:
            logger.error("Failed to extend Raft lock %s: %s", lock_key, e)
            return ExtendResult(success=False)

    def get_lock_info(self, path: str) -> LockInfo | None:
        lock_key = self._lock_key(path)
        try:
            store_info = self._store.get_lock_info(lock_key)
            if store_info is None:
                return None
            return self._store_info_to_lock_info(store_info)
        except Exception as e:
            logger.error("Failed to get lock info for %s: %s", lock_key, e)
            return None

    def list_locks(self, pattern: str = "", limit: int = 100) -> list[LockInfo]:
        try:
            store_locks = self._store.list_locks(prefix="", limit=limit)
            if store_locks is None:
                return []
            results = [self._store_info_to_lock_info(info) for info in store_locks]
            if pattern:
                results = [r for r in results if pattern in r.path]
            return results
        except Exception as e:
            logger.error("Failed to list locks: %s", e)
            return []

    def force_release(self, path: str) -> bool:
        lock_key = self._lock_key(path)
        try:
            released: bool = self._store.force_release_lock(lock_key)
            if released:
                logger.warning("Raft lock force-released: %s", lock_key)
            else:
                logger.debug("Raft lock force-release: no lock found for %s", lock_key)
            return released
        except Exception as e:
            logger.error("Failed to force-release Raft lock %s: %s", lock_key, e)
            return False

    def health_check(self) -> bool:
        try:
            self._store.get("/__health_check__")
            return True
        except Exception as e:
            logger.warning("Raft lock manager health check failed: %s", e)
            return False
