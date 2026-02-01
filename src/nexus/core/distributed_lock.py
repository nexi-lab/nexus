"""Distributed lock and semaphore manager interfaces and Redis implementation.

This module provides the lock/semaphore manager abstraction and Redis implementation
for coordinating access to resources across multiple Nexus nodes. It's part
of Block 2 (Issue #1106) for the distributed event system.

Architecture:
- LockManagerProtocol: Abstract interface for lock manager (mutex)
- SemaphoreManagerProtocol: Abstract interface for semaphore (N concurrent)
- RedisLockManager: Redis SET NX EX implementation for locks
- RedisSemaphoreManager: Redis Sorted Set implementation for semaphores (Issue #1160)
- Future: etcd, ZooKeeper, P2P implementations (Issue #1141)

Lock Implementation:
- Uses Redis SET NX EX pattern for atomic lock acquisition
- Lock key format: nexus:lock:{tenant_id}:{path}
- Lock value: lock_id (UUID) for ownership verification
- TTL-based auto-expiry prevents deadlocks from crashed clients

Semaphore Implementation (Issue #1160):
- Uses Redis Sorted Set for slot tracking
- Key format: nexus:semaphore:{tenant_id}:{resource}
- Members: slot_id (UUID), Scores: expiration timestamp
- Automatic cleanup of expired slots before each operation
- IMPORTANT: Uses coordination Dragonfly (noeviction) just like locks

Heartbeat Support:
- extend() method allows long-running operations to keep locks/semaphores alive
- If client crashes, lock/slot auto-expires after TTL (default: 30s)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

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
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot.

        Args:
            tenant_id: Tenant ID for the lock
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
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot.

        Args:
            tenant_id: Tenant ID for the lock
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

# =============================================================================
# Semaphore Lua Scripts (multi-slot lock)
# =============================================================================

# Lua script for atomic semaphore acquire
# KEYS[1] = semaphore key (ZSET), KEYS[2] = config key
# ARGV[1] = lock_id, ARGV[2] = max_holders, ARGV[3] = expire_ts, ARGV[4] = now_ts
SEMAPHORE_ACQUIRE_SCRIPT = """
-- 1. Clean up expired slots
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", ARGV[4])

-- 2. Check existing config (SSOT)
local existing_max = redis.call("GET", KEYS[2])
if existing_max then
    if tonumber(existing_max) ~= tonumber(ARGV[2]) then
        -- max_holders mismatch - return error code -1
        return -1
    end
else
    -- First holder sets the config
    redis.call("SET", KEYS[2], ARGV[2])
end

-- 3. Check current count
local count = redis.call("ZCARD", KEYS[1])
if count < tonumber(ARGV[2]) then
    -- 4. Has available slot, add holder
    redis.call("ZADD", KEYS[1], ARGV[3], ARGV[1])
    return 1
end
return 0
"""

# Lua script for atomic semaphore release with auto-cleanup
# KEYS[1] = semaphore key (ZSET), KEYS[2] = config key
# ARGV[1] = lock_id
SEMAPHORE_RELEASE_SCRIPT = """
local removed = redis.call("ZREM", KEYS[1], ARGV[1])
if removed == 1 then
    local remaining = redis.call("ZCARD", KEYS[1])
    if remaining == 0 then
        -- Last holder released, clean up config (SSOT auto-cleanup)
        redis.call("DEL", KEYS[2])
    end
end
return removed
"""

# Lua script for atomic semaphore extend
# KEYS[1] = semaphore key (ZSET)
# ARGV[1] = lock_id, ARGV[2] = new_expire_ts
SEMAPHORE_EXTEND_SCRIPT = """
-- Check if lock_id exists in ZSET
local score = redis.call("ZSCORE", KEYS[1], ARGV[1])
if score then
    -- Update expiration timestamp
    redis.call("ZADD", KEYS[1], ARGV[2], ARGV[1])
    return 1
end
return 0
"""


class RedisLockManager(LockManagerBase):
    """Redis-based distributed locking with heartbeat support.

    Uses Redis atomic operations for safe distributed locking.
    Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.

    Key formats:
    - Mutex: nexus:lock:{tenant_id}:{path} (String with SET NX EX)
    - Semaphore: nexus:semaphore:{tenant_id}:{path} (Sorted Set)
    - Semaphore config: nexus:semaphore_config:{tenant_id}:{path} (String, SSOT)

    Features:
    - Atomic acquisition using SET NX EX (mutex) or ZSET (semaphore)
    - Ownership verification on release/extend using Lua scripts
    - TTL-based auto-expiry (default: 30s)
    - Heartbeat support via extend() for long operations
    - SSOT config with auto-cleanup for semaphores

    Example (Mutex):
        >>> manager = RedisLockManager(redis_client)
        >>> lock_id = await manager.acquire("tenant", "/file.txt", timeout=5.0)
        >>> if lock_id:
        ...     try:
        ...         # Do exclusive work...
        ...         pass
        ...     finally:
        ...         await manager.release(lock_id, "tenant", "/file.txt")

    Example (Semaphore - Boardroom with 5 seats):
        >>> lock_id = await manager.acquire("tenant", "/room", max_holders=5)
        >>> if lock_id:
        ...     # One of 5 participants
        ...     await manager.release(lock_id, "tenant", "/room")
    """

    LOCK_PREFIX = "nexus:lock"
    SEMAPHORE_PREFIX = "nexus:semaphore"
    SEMAPHORE_CONFIG_PREFIX = "nexus:semaphore_config"

    # Exponential backoff parameters
    RETRY_BASE_INTERVAL = 0.05  # Start with 50ms
    RETRY_MAX_INTERVAL = 1.0  # Cap at 1 second
    RETRY_MULTIPLIER = 2.0  # Double each retry
    RETRY_JITTER = 0.5  # Add up to 50% random jitter

    def __init__(self, redis_client: DragonflyClient):
        """Initialize RedisLockManager.

        Args:
            redis_client: DragonflyClient instance for Redis connection
        """
        self._redis = redis_client
        # Mutex scripts
        self._release_script_sha: str | None = None
        self._extend_script_sha: str | None = None
        # Semaphore scripts
        self._sem_acquire_script_sha: str | None = None
        self._sem_release_script_sha: str | None = None
        self._sem_extend_script_sha: str | None = None

    def _lock_key(self, tenant_id: str, path: str) -> str:
        """Get Redis key for a mutex lock."""
        return f"{self.LOCK_PREFIX}:{tenant_id}:{path}"

    def _semaphore_key(self, tenant_id: str, path: str) -> str:
        """Get Redis key for a semaphore (ZSET)."""
        return f"{self.SEMAPHORE_PREFIX}:{tenant_id}:{path}"

    def _semaphore_config_key(self, tenant_id: str, path: str) -> str:
        """Get Redis key for semaphore config (SSOT for max_holders)."""
        return f"{self.SEMAPHORE_CONFIG_PREFIX}:{tenant_id}:{path}"

    async def _ensure_scripts_loaded(self) -> None:
        """Load Lua scripts into Redis if not already loaded."""
        # Mutex scripts
        if self._release_script_sha is None:
            self._release_script_sha = await self._redis.client.script_load(RELEASE_SCRIPT)
        if self._extend_script_sha is None:
            self._extend_script_sha = await self._redis.client.script_load(EXTEND_SCRIPT)
        # Semaphore scripts
        if self._sem_acquire_script_sha is None:
            self._sem_acquire_script_sha = await self._redis.client.script_load(
                SEMAPHORE_ACQUIRE_SCRIPT
            )
        if self._sem_release_script_sha is None:
            self._sem_release_script_sha = await self._redis.client.script_load(
                SEMAPHORE_RELEASE_SCRIPT
            )
        if self._sem_extend_script_sha is None:
            self._sem_extend_script_sha = await self._redis.client.script_load(
                SEMAPHORE_EXTEND_SCRIPT
            )

    async def acquire(
        self,
        tenant_id: str,
        path: str,
        timeout: float = LockManagerBase.DEFAULT_TIMEOUT,
        ttl: float = LockManagerBase.DEFAULT_TTL,
        max_holders: int = 1,
    ) -> str | None:
        """Acquire a distributed lock or semaphore slot on a path.

        Args:
            tenant_id: Tenant ID for the lock
            path: Path to lock (e.g., "/shared/config.json")
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds - auto-expires after this
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID (string) if acquired, None on timeout

        Raises:
            ValueError: If max_holders < 1 or max_holders mismatch (SSOT violation)
        """
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        # DRY: Route to appropriate implementation
        if max_holders == 1:
            return await self._acquire_mutex(tenant_id, path, timeout, ttl)
        else:
            return await self._acquire_semaphore(tenant_id, path, timeout, ttl, max_holders)

    async def _acquire_mutex(
        self,
        tenant_id: str,
        path: str,
        timeout: float,
        ttl: float,
    ) -> str | None:
        """Acquire an exclusive mutex lock using SET NX EX."""
        key = self._lock_key(tenant_id, path)
        lock_id = str(uuid.uuid4())
        ttl_ms = int(ttl * 1000)

        deadline = asyncio.get_event_loop().time() + timeout
        retry_interval = self.RETRY_BASE_INTERVAL

        while True:
            # Try to acquire lock atomically
            acquired = await self._redis.client.set(
                key,
                lock_id,
                nx=True,  # Only set if not exists
                px=ttl_ms,  # Expiry in milliseconds
            )

            if acquired:
                logger.debug(f"Mutex acquired: {key} -> {lock_id} (TTL: {ttl}s)")
                return lock_id

            # Check if we've exceeded timeout
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.debug(f"Mutex acquisition timeout: {key}")
                return None

            # Exponential backoff with jitter to reduce thundering herd
            jitter = random.uniform(0, retry_interval * self.RETRY_JITTER)
            sleep_time = min(retry_interval + jitter, remaining)
            await asyncio.sleep(sleep_time)

            # Increase interval for next retry (exponential backoff)
            retry_interval = min(
                retry_interval * self.RETRY_MULTIPLIER,
                self.RETRY_MAX_INTERVAL,
            )

    async def _acquire_semaphore(
        self,
        tenant_id: str,
        path: str,
        timeout: float,
        ttl: float,
        max_holders: int,
    ) -> str | None:
        """Acquire a semaphore slot using ZSET with Lua script."""
        await self._ensure_scripts_loaded()
        assert self._sem_acquire_script_sha is not None

        sem_key = self._semaphore_key(tenant_id, path)
        config_key = self._semaphore_config_key(tenant_id, path)
        lock_id = str(uuid.uuid4())

        deadline = asyncio.get_event_loop().time() + timeout
        retry_interval = self.RETRY_BASE_INTERVAL

        while True:
            now_ts = time.time()
            expire_ts = now_ts + ttl

            # Try to acquire slot atomically via Lua script
            result = await cast(
                Awaitable[int],
                self._redis.client.evalsha(
                    self._sem_acquire_script_sha,
                    2,  # Number of keys
                    sem_key,  # KEYS[1]
                    config_key,  # KEYS[2]
                    lock_id,  # ARGV[1]
                    max_holders,  # ARGV[2]
                    expire_ts,  # ARGV[3]
                    now_ts,  # ARGV[4]
                ),
            )

            if result == 1:
                logger.debug(
                    f"Semaphore slot acquired: {sem_key} -> {lock_id} "
                    f"(max_holders={max_holders}, TTL={ttl}s)"
                )
                return lock_id
            elif result == -1:
                # SSOT violation: max_holders mismatch
                existing_max = await self._redis.client.get(config_key)
                existing_val = (
                    int(existing_max.decode() if isinstance(existing_max, bytes) else existing_max)
                    if existing_max
                    else "unknown"
                )
                raise ValueError(
                    f"max_holders mismatch for {path}: expected {existing_val}, got {max_holders}"
                )

            # result == 0: No available slot, retry
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.debug(f"Semaphore acquisition timeout: {sem_key}")
                return None

            # Exponential backoff with jitter
            jitter = random.uniform(0, retry_interval * self.RETRY_JITTER)
            sleep_time = min(retry_interval + jitter, remaining)
            await asyncio.sleep(sleep_time)

            retry_interval = min(
                retry_interval * self.RETRY_MULTIPLIER,
                self.RETRY_MAX_INTERVAL,
            )

    async def release(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock or semaphore slot.

        Only releases if caller owns the lock (lock_id matches).
        Automatically detects whether this is a mutex or semaphore.

        Args:
            lock_id: Lock ID from acquire()
            tenant_id: Tenant ID
            path: Path that was locked

        Returns:
            True if released, False if not owned or expired
        """
        await self._ensure_scripts_loaded()
        assert self._release_script_sha is not None
        assert self._sem_release_script_sha is not None

        # Try mutex first (most common case)
        mutex_key = self._lock_key(tenant_id, path)

        try:
            result = await cast(
                Awaitable[int],
                self._redis.client.evalsha(
                    self._release_script_sha,
                    1,  # Number of keys
                    mutex_key,  # KEYS[1]
                    lock_id,  # ARGV[1]
                ),
            )

            if result == 1:
                logger.debug(f"Mutex released: {mutex_key}")
                return True

        except Exception as e:
            logger.error(f"Failed to release mutex {mutex_key}: {e}")
            # Fall through to try semaphore

        # Try semaphore if mutex didn't match
        sem_key = self._semaphore_key(tenant_id, path)
        config_key = self._semaphore_config_key(tenant_id, path)

        try:
            result = await cast(
                Awaitable[int],
                self._redis.client.evalsha(
                    self._sem_release_script_sha,
                    2,  # Number of keys
                    sem_key,  # KEYS[1]
                    config_key,  # KEYS[2]
                    lock_id,  # ARGV[1]
                ),
            )

            if result == 1:
                logger.debug(f"Semaphore slot released: {sem_key}")
                return True

            logger.debug(f"Release failed (not owned or expired): {path}")
            return False

        except Exception as e:
            logger.error(f"Failed to release semaphore {sem_key}: {e}")
            return False

    async def extend(
        self,
        lock_id: str,
        tenant_id: str,
        path: str,
        ttl: float = LockManagerBase.DEFAULT_TTL,
    ) -> bool:
        """Extend a lock's or semaphore slot's TTL (heartbeat).

        Use this to keep locks alive during long-running operations.
        Automatically detects whether this is a mutex or semaphore.

        Args:
            lock_id: Lock ID from acquire()
            tenant_id: Tenant ID
            path: Path that was locked
            ttl: New TTL in seconds

        Returns:
            True if extended, False if not owned or expired
        """
        await self._ensure_scripts_loaded()
        assert self._extend_script_sha is not None
        assert self._sem_extend_script_sha is not None

        # Try mutex first (most common case)
        mutex_key = self._lock_key(tenant_id, path)
        ttl_seconds = int(ttl)

        try:
            result = await cast(
                Awaitable[int],
                self._redis.client.evalsha(
                    self._extend_script_sha,
                    1,  # Number of keys
                    mutex_key,  # KEYS[1]
                    lock_id,  # ARGV[1]
                    ttl_seconds,  # ARGV[2]
                ),
            )

            if result == 1:
                logger.debug(f"Mutex extended: {mutex_key} (new TTL: {ttl}s)")
                return True

        except Exception as e:
            logger.error(f"Failed to extend mutex {mutex_key}: {e}")
            # Fall through to try semaphore

        # Try semaphore if mutex didn't match
        sem_key = self._semaphore_key(tenant_id, path)
        new_expire_ts = time.time() + ttl

        try:
            result = await cast(
                Awaitable[int],
                self._redis.client.evalsha(
                    self._sem_extend_script_sha,
                    1,  # Number of keys
                    sem_key,  # KEYS[1]
                    lock_id,  # ARGV[1]
                    new_expire_ts,  # ARGV[2]
                ),
            )

            if result == 1:
                logger.debug(f"Semaphore slot extended: {sem_key} (new TTL: {ttl}s)")
                return True

            logger.debug(f"Extend failed (not owned or expired): {path}")
            return False

        except Exception as e:
            logger.error(f"Failed to extend semaphore {sem_key}: {e}")
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
