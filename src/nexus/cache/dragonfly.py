"""Dragonfly (Redis-compatible) cache backend implementation.

Dragonfly is a Redis-compatible in-memory datastore that provides:
- 25x throughput improvement over Redis
- 80% less memory usage
- Multi-threaded architecture
- Smart eviction with cache_mode=true

This module provides:
- DragonflyClient: Connection pool manager
- DragonflyCacheStore: CacheStoreABC driver (Fourth Pillar production backend)
- Domain caches: Permission, Tiger, ResourceMap, Embedding

Connection Pool Optimizations (Issue #1075):
- BlockingConnectionPool: Waits for available connections instead of erroring
- TCP keepalive: Prevents NAT/firewall from dropping idle connections
- Retry on timeout: Automatic retry for transient network issues
"""

import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from nexus.contracts.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)

# Redis client is optional - only imported if Dragonfly is configured
try:
    import redis.asyncio as redis
    from redis.asyncio import BlockingConnectionPool
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from redis.retry import Retry

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None  # type: ignore
    BlockingConnectionPool = None  # type: ignore


class DragonflyClient:
    """Managed Dragonfly/Redis connection with health checks and reconnection.

    Provides a connection pool and health monitoring for Dragonfly connections.
    Uses redis-py async client which is compatible with Dragonfly.

    Connection Pool Features (Issue #1075):
    - BlockingConnectionPool: Waits for connections instead of raising errors
    - TCP keepalive: Detects dead connections through firewalls/NAT
    - Automatic retry: Retries on timeout and connection errors
    - Configurable via environment variables

    Example:
        async with DragonflyClient("redis://localhost:6379") as client:
            await client.client.set("key", "value")
            value = await client.client.get("key")

    Environment Variables:
        NEXUS_REDIS_POOL_SIZE: Max connections in pool (default: 50)
        NEXUS_REDIS_SOCKET_TIMEOUT: Socket timeout in seconds (default: 30)
        NEXUS_REDIS_CONNECT_TIMEOUT: Connection timeout in seconds (default: 5)
        NEXUS_REDIS_POOL_TIMEOUT: Wait time for available connection (default: 20)
        NEXUS_REDIS_KEEPALIVE: Enable TCP keepalive (default: true)
        NEXUS_REDIS_RETRY_ON_TIMEOUT: Retry on timeout errors (default: true)
    """

    def __init__(
        self,
        url: str,
        pool_size: int = 50,
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
        pool_timeout: float = 20.0,
        socket_keepalive: bool = True,
        retry_on_timeout: bool = True,
    ):
        """Initialize Dragonfly client.

        Args:
            url: Redis-compatible URL (e.g., redis://localhost:6379)
            pool_size: Maximum connections in pool (default: 50)
            timeout: Socket timeout in seconds (default: 30)
            connect_timeout: Connection timeout in seconds (default: 5)
            pool_timeout: Seconds to wait for available connection (default: 20)
            socket_keepalive: Enable TCP keepalive (default: True)
            retry_on_timeout: Retry on timeout errors (default: True)
        """
        if not REDIS_AVAILABLE:
            raise ImportError("redis package not installed. Install with: pip install redis")

        self._url = url
        self._pool_size = pool_size
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._pool_timeout = pool_timeout
        self._socket_keepalive = socket_keepalive
        self._retry_on_timeout = retry_on_timeout
        self._pool: Any = None
        self._client: Any = None
        self._connected = False

    async def connect(self) -> None:
        """Initialize connection pool and verify connectivity."""
        if self._connected:
            return

        # Build pool kwargs
        pool_kwargs: dict = {
            "max_connections": self._pool_size,
            "socket_timeout": self._timeout,
            "socket_connect_timeout": self._connect_timeout,
            "timeout": self._pool_timeout,  # BlockingConnectionPool wait timeout
            "decode_responses": False,  # Binary data for bitmaps
            "retry_on_timeout": self._retry_on_timeout,
        }

        # TCP keepalive settings for cloud/NAT environments
        # Cloud NAT gateways (AWS, GCP) have ~350s idle timeouts
        if self._socket_keepalive:
            pool_kwargs["socket_keepalive"] = True
            # Platform-specific keepalive options
            try:
                keepalive_options: dict[int, int] = {}
                # TCP_KEEPIDLE may not be available on all platforms (e.g., macOS)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    keepalive_options[socket.TCP_KEEPIDLE] = 60  # Start probes after 60s idle
                if hasattr(socket, "TCP_KEEPINTVL"):
                    keepalive_options[socket.TCP_KEEPINTVL] = 10  # Probe every 10s
                if hasattr(socket, "TCP_KEEPCNT"):
                    keepalive_options[socket.TCP_KEEPCNT] = 3  # 3 failed probes = dead
                if keepalive_options:
                    pool_kwargs["socket_keepalive_options"] = keepalive_options
            except AttributeError:
                # Some platforms (e.g., macOS) may not have all options
                logger.debug("TCP keepalive options not fully supported on this platform")

        # Use BlockingConnectionPool to wait for available connections
        # instead of raising "Too many connections" errors
        self._pool = BlockingConnectionPool.from_url(self._url, **pool_kwargs)

        # Configure retry strategy
        client_kwargs: dict = {"connection_pool": self._pool}
        if self._retry_on_timeout:
            # Exponential backoff retry: 3 retries with backoff
            retry = Retry(ExponentialBackoff(), retries=3)
            client_kwargs["retry"] = retry
            client_kwargs["retry_on_error"] = [RedisConnectionError, RedisTimeoutError]

        self._client = redis.Redis(**client_kwargs)

        # Verify connection
        try:
            await self._client.ping()
            self._connected = True
            logger.info(
                f"Connected to Dragonfly at {self._safe_url} "
                f"(pool_size={self._pool_size}, keepalive={self._socket_keepalive})"
            )
        except Exception as e:
            await self.disconnect()
            raise ConnectionError(f"Failed to connect to Dragonfly: {e}") from e

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._pool:
            await self._pool.disconnect()
            self._pool = None
        self._connected = False
        logger.info("Disconnected from Dragonfly")

    @property
    def client(self) -> Any:
        """Get the Redis client (redis.asyncio.Redis).

        Raises:
            RuntimeError: If not connected
        """
        if not self._client or not self._connected:
            raise RuntimeError("DragonflyClient not connected. Call connect() first.")
        return self._client

    @property
    def _safe_url(self) -> str:
        """Get URL with password masked for logging."""
        if "@" in self._url:
            parts = self._url.split("@")
            return f"{parts[0].split(':')[0]}:***@{parts[1]}"
        return self._url

    async def health_check(self) -> bool:
        """Check if connection is healthy.

        Returns:
            True if connection is healthy, False otherwise
        """
        if not self._client or not self._connected:
            return False
        try:
            await self._client.ping()
            return True
        except Exception as e:
            logger.warning(f"Dragonfly health check failed: {e}")
            return False

    async def get_info(self) -> dict:
        """Get Dragonfly server info.

        Returns:
            Dict with server info (version, memory, etc.)
        """
        if not self._client:
            return {"status": "disconnected"}
        try:
            info = await self._client.info()
            result = {
                "status": "connected",
                "version": info.get("redis_version", "unknown"),
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "connected_clients": info.get("connected_clients", 0),
            }
            # Add pool stats
            result.update(self.get_pool_stats())
            return result
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_pool_stats(self) -> dict:
        """Get connection pool statistics.

        Returns:
            Dict with pool statistics including:
            - pool_max_connections: Maximum connections configured
            - pool_current_connections: Current connections in pool
            - pool_available_connections: Available connections
            - pool_in_use_connections: Connections currently in use
        """
        if not self._pool:
            return {"pool_status": "not_initialized"}

        try:
            # BlockingConnectionPool attributes
            stats = {
                "pool_status": "active",
                "pool_max_connections": self._pool_size,
                "pool_timeout": self._pool_timeout,
                "socket_keepalive": self._socket_keepalive,
                "retry_on_timeout": self._retry_on_timeout,
            }

            # Try to get connection counts (implementation-specific)
            if hasattr(self._pool, "_available_connections"):
                stats["pool_available_connections"] = len(self._pool._available_connections)
            if hasattr(self._pool, "_in_use_connections"):
                stats["pool_in_use_connections"] = len(self._pool._in_use_connections)
            if hasattr(self._pool, "max_connections"):
                stats["pool_max_connections"] = self._pool.max_connections

            return stats
        except Exception as e:
            return {"pool_status": "error", "error": str(e)}

    async def __aenter__(self) -> "DragonflyClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.disconnect()


class DragonflyCacheStore(CacheStoreABC):
    """Dragonfly (Redis-compatible) driver for CacheStoreABC — the production backend.

    Wraps DragonflyClient to implement the low-level KV + PubSub primitives
    that domain caches (PermissionCache, TigerCache, EventBus) are built upon.

    OS Analogy: /dev/shm backed by a real tmpfs mount (vs NullCacheStore = CONFIG_FSCACHE=n).

    Usage:
        client = DragonflyClient("redis://localhost:6379")
        await client.connect()
        store = DragonflyCacheStore(client)

        await store.set("key", b"value", ttl=300)
        data = await store.get("key")

        async with store.subscribe("events:zone1") as messages:
            async for msg in messages:
                process(msg)
    """

    def __init__(self, client: "DragonflyClient") -> None:
        self._client = client

    # --- KV operations ---

    async def get(self, key: str) -> bytes | None:
        result: bytes | None = await self._client.client.get(key)
        return result

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        if ttl is not None:
            await self._client.client.setex(key, ttl, value)
        else:
            await self._client.client.set(key, value)

    async def delete(self, key: str) -> bool:
        result = await self._client.client.delete(key)
        return bool(result > 0)

    async def exists(self, key: str) -> bool:
        result = await self._client.client.exists(key)
        return bool(result > 0)

    async def delete_by_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern using SCAN + pipeline DEL (Decision #13)."""
        deleted = 0
        batch: list[bytes | str] = []
        async for key in self._client.client.scan_iter(match=pattern, count=1000):
            batch.append(key)
            if len(batch) >= 1000:
                pipe = self._client.client.pipeline()
                for k in batch:
                    pipe.delete(k)
                results = await pipe.execute()
                deleted += sum(1 for r in results if r)
                batch.clear()
        # Flush remaining batch
        if batch:
            pipe = self._client.client.pipeline()
            for k in batch:
                pipe.delete(k)
            results = await pipe.execute()
            deleted += sum(1 for r in results if r)
        return deleted

    async def keys_by_pattern(self, pattern: str) -> list[str]:
        """Return keys matching pattern using SCAN cursor."""
        result: list[str] = []
        async for key in self._client.client.scan_iter(match=pattern, count=1000):
            result.append(key.decode() if isinstance(key, bytes) else key)
        return result

    # --- Batch KV operations (Decision #13) ---

    async def get_many(self, keys: list[str]) -> dict[str, bytes | None]:
        """Batch get using Redis MGET instead of N sequential GETs."""
        if not keys:
            return {}
        try:
            values = await self._client.client.mget(keys)
            return dict(zip(keys, values, strict=True))
        except Exception:
            # Fallback to sequential gets
            logger.warning("[DragonflyCacheStore] MGET failed, falling back to sequential")
            return {k: await self.get(k) for k in keys}

    async def set_many(self, mapping: dict[str, bytes], ttl: int | None = None) -> None:
        """Batch set using Redis pipeline instead of N sequential SETs."""
        if not mapping:
            return
        pipe = self._client.client.pipeline()
        for k, v in mapping.items():
            if ttl is not None:
                pipe.setex(k, ttl, v)
            else:
                pipe.set(k, v)
        await pipe.execute()

    # --- PubSub operations ---

    async def publish(self, channel: str, message: bytes) -> int:
        result: int = await self._client.client.publish(channel, message)
        return result

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[bytes]]:
        pubsub = self._client.client.pubsub()
        await pubsub.subscribe(channel)
        try:

            async def _messages() -> AsyncIterator[bytes]:
                async for msg in pubsub.listen():
                    if msg["type"] == "message":
                        yield msg["data"]

            yield _messages()
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    # --- Lifecycle ---

    async def health_check(self) -> bool:
        return await self._client.health_check()

    async def close(self) -> None:
        await self._client.disconnect()
