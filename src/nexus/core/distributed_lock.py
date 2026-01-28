"""Distributed lock manager interfaces and Redis implementation.

This module provides the lock manager abstraction and Redis implementation
for coordinating access to resources across multiple Nexus nodes. It's part
of Block 2 (Issue #1106) for the distributed event system.

Architecture:
- LockManagerProtocol: Abstract interface for lock manager implementations
- RedisLockManager: Redis SET NX EX implementation (default)
- Future: etcd, ZooKeeper, P2P implementations (Issue #1141)

Lock Implementation:
- Uses Redis SET NX EX pattern for atomic lock acquisition
- Lock key format: nexus:lock:{tenant_id}:{path}
- Lock value: lock_id (UUID) for ownership verification
- TTL-based auto-expiry prevents deadlocks from crashed clients

Heartbeat Support:
- extend() method allows long-running operations to keep locks alive
- If client crashes, lock auto-expires after TTL (default: 30s)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.cache.dragonfly import DragonflyClient

logger = logging.getLogger(__name__)


# =============================================================================
# Abstract Interface (Protocol)
# =============================================================================


@runtime_checkable
class LockManagerProtocol(Protocol):
    """Protocol defining the lock manager interface.

    This protocol allows different backend implementations:
    - Redis SET NX EX (default, implemented as RedisLockManager)
    - etcd lease-based locks (future)
    - ZooKeeper ephemeral nodes (future)
    - P2P consensus locks (future)

    All implementations must provide these async methods.
    """

    async def acquire(
        self,
        tenant_id: str,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> str | None:
        """Acquire a distributed lock.

        Args:
            tenant_id: Tenant ID for the lock
            path: Path to lock
            timeout: Maximum time to wait for lock
            ttl: Lock TTL (auto-expires after this)

        Returns:
            Lock ID if acquired, None on timeout
        """
        ...

    async def release(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock.

        Args:
            lock_id: Lock ID from acquire()
            tenant_id: Tenant ID
            path: Path that was locked

        Returns:
            True if released, False if not owned or expired
        """
        ...

    async def extend(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
        ttl: float = 30.0,
    ) -> bool:
        """Extend lock TTL (heartbeat).

        Args:
            lock_id: Lock ID from acquire()
            tenant_id: Tenant ID
            path: Path that was locked
            ttl: New TTL in seconds

        Returns:
            True if extended, False if not owned or expired
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
        tenant_id: str,
        path: str,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL,
    ) -> str | None:
        """Acquire a distributed lock."""
        pass

    @abstractmethod
    async def release(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock."""
        pass

    @abstractmethod
    async def extend(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
        ttl: float = DEFAULT_TTL,
    ) -> bool:
        """Extend lock TTL (heartbeat)."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        pass

    async def is_locked(self, tenant_id: str, path: str) -> bool:
        """Check if a path is currently locked. Override for efficiency."""
        info = await self.get_lock_info(tenant_id, path)
        return info is not None

    async def get_lock_info(
        self,
        tenant_id: str,  # noqa: ARG002
        path: str,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Get information about a lock. Override in subclasses."""
        return None


# =============================================================================
# Redis Implementation
# =============================================================================

# Lua script for atomic release: check owner then delete
RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Lua script for atomic extend: check owner then set new expiry
EXTEND_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLockManager(LockManagerBase):
    """Redis-based distributed locking with heartbeat support.

    Uses Redis atomic operations for safe distributed locking.
    Key format: nexus:lock:{tenant_id}:{path}

    Features:
    - Atomic acquisition using SET NX EX
    - Ownership verification on release/extend using Lua scripts
    - TTL-based auto-expiry (default: 30s)
    - Heartbeat support via extend() for long operations

    Example:
        >>> manager = RedisLockManager(redis_client)
        >>>
        >>> # Acquire lock
        >>> lock_id = await manager.acquire("tenant", "/file.txt", timeout=5.0)
        >>> if lock_id:
        ...     try:
        ...         # Do exclusive work...
        ...         pass
        ...     finally:
        ...         await manager.release(lock_id, "tenant", "/file.txt")

    Meeting Floor Control Example:
        >>> lock_id = await manager.acquire("tenant", "/meeting/floor", timeout=5.0)
        >>> if lock_id:
        ...     # Start heartbeat in background
        ...     async def heartbeat():
        ...         while speaking:
        ...             await manager.extend(lock_id, "tenant", "/meeting/floor")
        ...             await asyncio.sleep(15)
        ...     task = asyncio.create_task(heartbeat())
        ...     # Do speech...
        ...     task.cancel()
        ...     await manager.release(lock_id, "tenant", "/meeting/floor")
    """

    LOCK_PREFIX = "nexus:lock"
    RETRY_INTERVAL = 0.1  # Retry interval when waiting for lock

    def __init__(self, redis_client: DragonflyClient):
        """Initialize RedisLockManager.

        Args:
            redis_client: DragonflyClient instance for Redis connection
        """
        self._redis = redis_client
        self._release_script_sha: str | None = None
        self._extend_script_sha: str | None = None

    def _lock_key(self, tenant_id: str, path: str) -> str:
        """Get Redis key for a lock."""
        return f"{self.LOCK_PREFIX}:{tenant_id}:{path}"

    async def _ensure_scripts_loaded(self) -> None:
        """Load Lua scripts into Redis if not already loaded."""
        if self._release_script_sha is None:
            self._release_script_sha = await self._redis.client.script_load(RELEASE_SCRIPT)
        if self._extend_script_sha is None:
            self._extend_script_sha = await self._redis.client.script_load(EXTEND_SCRIPT)

    async def acquire(
        self,
        tenant_id: str,
        path: str,
        timeout: float = LockManagerBase.DEFAULT_TIMEOUT,
        ttl: float = LockManagerBase.DEFAULT_TTL,
    ) -> str | None:
        """Acquire a distributed lock on a path.

        Args:
            tenant_id: Tenant ID for the lock
            path: Path to lock (e.g., "/shared/config.json")
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds - auto-expires after this

        Returns:
            Lock ID (string) if acquired, None on timeout
        """
        key = self._lock_key(tenant_id, path)
        lock_id = str(uuid.uuid4())
        ttl_ms = int(ttl * 1000)

        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            # Try to acquire lock atomically
            acquired = await self._redis.client.set(
                key,
                lock_id,
                nx=True,  # Only set if not exists
                px=ttl_ms,  # Expiry in milliseconds
            )

            if acquired:
                logger.debug(f"Lock acquired: {key} -> {lock_id} (TTL: {ttl}s)")
                return lock_id

            # Check if we've exceeded timeout
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.debug(f"Lock acquisition timeout: {key}")
                return None

            # Wait before retry
            await asyncio.sleep(min(self.RETRY_INTERVAL, remaining))

    async def release(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock.

        Only releases if caller owns the lock (lock_id matches).

        Args:
            lock_id: Lock ID from acquire()
            tenant_id: Tenant ID
            path: Path that was locked

        Returns:
            True if released, False if not owned or expired
        """
        await self._ensure_scripts_loaded()
        assert self._release_script_sha is not None  # Guaranteed by _ensure_scripts_loaded

        key = self._lock_key(tenant_id, path)

        try:
            result = await self._redis.client.evalsha(
                self._release_script_sha,
                1,  # Number of keys
                key,  # KEYS[1]
                lock_id,  # ARGV[1]
            )

            released: bool = result == 1
            if released:
                logger.debug(f"Lock released: {key}")
            else:
                logger.debug(f"Lock release failed (not owned or expired): {key}")

            return released

        except Exception as e:
            logger.error(f"Failed to release lock {key}: {e}")
            return False

    async def extend(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
        ttl: float = LockManagerBase.DEFAULT_TTL,
    ) -> bool:
        """Extend a lock's TTL (heartbeat).

        Use this to keep locks alive during long-running operations.

        Args:
            lock_id: Lock ID from acquire()
            tenant_id: Tenant ID
            path: Path that was locked
            ttl: New TTL in seconds

        Returns:
            True if extended, False if not owned or expired
        """
        await self._ensure_scripts_loaded()
        assert self._extend_script_sha is not None  # Guaranteed by _ensure_scripts_loaded

        key = self._lock_key(tenant_id, path)
        ttl_seconds = int(ttl)

        try:
            result = await self._redis.client.evalsha(
                self._extend_script_sha,
                1,  # Number of keys
                key,  # KEYS[1]
                lock_id,  # ARGV[1]
                ttl_seconds,  # ARGV[2]
            )

            extended: bool = result == 1
            if extended:
                logger.debug(f"Lock extended: {key} (new TTL: {ttl}s)")
            else:
                logger.debug(f"Lock extend failed (not owned or expired): {key}")

            return extended

        except Exception as e:
            logger.error(f"Failed to extend lock {key}: {e}")
            return False

    async def is_locked(self, tenant_id: str, path: str) -> bool:
        """Check if a path is currently locked."""
        key = self._lock_key(tenant_id, path)
        exists_count: int = await self._redis.client.exists(key)
        return exists_count > 0

    async def get_lock_info(self, tenant_id: str, path: str) -> dict[str, Any] | None:
        """Get information about a lock.

        Returns:
            Dict with lock info if locked, None if not locked
        """
        key = self._lock_key(tenant_id, path)

        lock_id = await self._redis.client.get(key)
        if lock_id is None:
            return None

        ttl = await self._redis.client.ttl(key)

        return {
            "lock_id": lock_id.decode("utf-8") if isinstance(lock_id, bytes) else lock_id,
            "ttl": ttl,
            "tenant_id": tenant_id,
            "path": path,
        }

    async def force_release(self, tenant_id: str, path: str) -> bool:
        """Force release a lock regardless of owner.

        WARNING: Administrative operation for stuck lock recovery only.

        Args:
            tenant_id: Tenant ID
            path: Path to unlock

        Returns:
            True if deleted, False if not found
        """
        key = self._lock_key(tenant_id, path)
        deleted: int = await self._redis.client.delete(key)

        if deleted:
            logger.warning(f"Lock force-released: {key}")
        return deleted > 0

    async def health_check(self) -> bool:
        """Check if the lock manager is healthy."""
        try:
            return await self._redis.health_check()
        except Exception as e:
            logger.warning(f"Lock manager health check failed: {e}")
            return False


# Backward compatibility alias
DistributedLockManager = RedisLockManager


# =============================================================================
# Factory and Singleton Management
# =============================================================================


def create_lock_manager(
    backend: str = "redis",
    redis_client: DragonflyClient | None = None,
    **kwargs: Any,  # noqa: ARG001 - Reserved for future backends
) -> LockManagerBase:
    """Factory function to create a lock manager instance.

    Args:
        backend: Backend type ("redis", future: "etcd", "zookeeper", "p2p")
        redis_client: DragonflyClient for Redis backend
        **kwargs: Additional backend-specific arguments

    Returns:
        LockManagerBase implementation

    Raises:
        ValueError: If backend is not supported
        ValueError: If required arguments are missing
    """
    if backend == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required for Redis backend")
        return RedisLockManager(redis_client)

    # Future backends
    # elif backend == "etcd":
    #     return EtcdLockManager(...)
    # elif backend == "zookeeper":
    #     return ZooKeeperLockManager(...)
    # elif backend == "p2p":
    #     return P2PLockManager(...)

    raise ValueError(f"Unsupported lock manager backend: {backend}")


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
