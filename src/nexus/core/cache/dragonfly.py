"""Dragonfly (Redis-compatible) cache backend implementation.

Dragonfly is a Redis-compatible in-memory datastore that provides:
- 25x throughput improvement over Redis
- 80% less memory usage
- Multi-threaded architecture
- Smart eviction with cache_mode=true

This module provides the client connection manager and cache implementations.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Redis client is optional - only imported if Dragonfly is configured
try:
    import redis.asyncio as redis
    from redis.asyncio import ConnectionPool

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None  # type: ignore
    ConnectionPool = None  # type: ignore


class DragonflyClient:
    """Managed Dragonfly/Redis connection with health checks and reconnection.

    Provides a connection pool and health monitoring for Dragonfly connections.
    Uses redis-py async client which is compatible with Dragonfly.

    Example:
        async with DragonflyClient("redis://localhost:6379") as client:
            await client.client.set("key", "value")
            value = await client.client.get("key")
    """

    def __init__(
        self,
        url: str,
        pool_size: int = 10,
        timeout: float = 5.0,
    ):
        """Initialize Dragonfly client.

        Args:
            url: Redis-compatible URL (e.g., redis://localhost:6379)
            pool_size: Maximum connections in pool
            timeout: Socket timeout in seconds
        """
        if not REDIS_AVAILABLE:
            raise ImportError(
                "redis package not installed. Install with: pip install redis"
            )

        self._url = url
        self._pool_size = pool_size
        self._timeout = timeout
        self._pool: Optional[ConnectionPool] = None
        self._client: Optional[redis.Redis] = None
        self._connected = False

    async def connect(self) -> None:
        """Initialize connection pool and verify connectivity."""
        if self._connected:
            return

        self._pool = redis.ConnectionPool.from_url(
            self._url,
            max_connections=self._pool_size,
            socket_timeout=self._timeout,
            socket_connect_timeout=self._timeout,
            decode_responses=False,  # Binary data for bitmaps
        )
        self._client = redis.Redis(connection_pool=self._pool)

        # Verify connection
        try:
            await self._client.ping()
            self._connected = True
            logger.info(f"Connected to Dragonfly at {self._safe_url}")
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
    def client(self) -> "redis.Redis":
        """Get the Redis client.

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
            return {
                "status": "connected",
                "version": info.get("redis_version", "unknown"),
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "connected_clients": info.get("connected_clients", 0),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def __aenter__(self) -> "DragonflyClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *args) -> None:
        """Async context manager exit."""
        await self.disconnect()


class DragonflyPermissionCache:
    """Dragonfly-backed permission cache.

    Stores permission check results with TTL. Grants and denials have
    different TTLs (denials expire faster for security).

    Key format:
        perm:{tenant_id}:{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}

    Value:
        "1" for grant, "0" for denial
    """

    def __init__(
        self,
        client: DragonflyClient,
        ttl: int = 300,
        denial_ttl: int = 60,
    ):
        """Initialize permission cache.

        Args:
            client: DragonflyClient instance
            ttl: TTL for grants in seconds (default: 5 minutes)
            denial_ttl: TTL for denials in seconds (default: 1 minute)
        """
        self._client = client
        self._ttl = ttl
        self._denial_ttl = denial_ttl

    def _make_key(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str,
    ) -> str:
        """Create cache key for permission entry."""
        return f"perm:{tenant_id}:{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}"

    async def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str,
    ) -> Optional[bool]:
        """Get cached permission result."""
        key = self._make_key(
            subject_type, subject_id, permission, object_type, object_id, tenant_id
        )
        value = await self._client.client.get(key)
        if value is None:
            return None
        return value == b"1"

    async def set(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        tenant_id: str,
    ) -> None:
        """Cache permission result."""
        key = self._make_key(
            subject_type, subject_id, permission, object_type, object_id, tenant_id
        )
        ttl = self._ttl if result else self._denial_ttl
        await self._client.client.setex(key, ttl, "1" if result else "0")

    async def invalidate_subject(
        self,
        subject_type: str,
        subject_id: str,
        tenant_id: str,
    ) -> int:
        """Invalidate all permissions for a subject."""
        pattern = f"perm:{tenant_id}:{subject_type}:{subject_id}:*"
        return await self._delete_by_pattern(pattern)

    async def invalidate_object(
        self,
        object_type: str,
        object_id: str,
        tenant_id: str,
    ) -> int:
        """Invalidate all permissions for an object."""
        # Need to scan all subjects - use pattern with wildcards
        pattern = f"perm:{tenant_id}:*:*:*:{object_type}:{object_id}"
        return await self._delete_by_pattern(pattern)

    async def invalidate_subject_object(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        tenant_id: str,
    ) -> int:
        """Invalidate permissions for a specific subject-object pair."""
        pattern = f"perm:{tenant_id}:{subject_type}:{subject_id}:*:{object_type}:{object_id}"
        return await self._delete_by_pattern(pattern)

    async def clear(self, tenant_id: Optional[str] = None) -> int:
        """Clear all cached permissions."""
        if tenant_id:
            pattern = f"perm:{tenant_id}:*"
        else:
            pattern = "perm:*"
        return await self._delete_by_pattern(pattern)

    async def _delete_by_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern using SCAN (non-blocking)."""
        deleted = 0
        async for key in self._client.client.scan_iter(match=pattern, count=1000):
            await self._client.client.delete(key)
            deleted += 1
        return deleted

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        return await self._client.health_check()

    async def get_stats(self) -> dict:
        """Get cache statistics."""
        info = await self._client.get_info()
        return {
            "backend": "dragonfly",
            "status": info.get("status", "unknown"),
            "ttl_grants": self._ttl,
            "ttl_denials": self._denial_ttl,
        }


class DragonflyTigerCache:
    """Dragonfly-backed Tiger cache for pre-materialized permissions.

    Stores Roaring Bitmap data for O(1) list filtering.

    Key format:
        tiger:{tenant_id}:{subject_type}:{subject_id}:{permission}:{resource_type}

    Value:
        Hash with fields:
        - data: Serialized Roaring Bitmap bytes
        - revision: Integer revision for staleness detection
    """

    def __init__(
        self,
        client: DragonflyClient,
        ttl: int = 3600,
    ):
        """Initialize Tiger cache.

        Args:
            client: DragonflyClient instance
            ttl: TTL in seconds (default: 1 hour)
        """
        self._client = client
        self._ttl = ttl

    def _make_key(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
    ) -> str:
        """Create cache key for Tiger entry."""
        return f"tiger:{tenant_id}:{subject_type}:{subject_id}:{permission}:{resource_type}"

    async def get_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
    ) -> Optional[tuple[bytes, int]]:
        """Get Tiger bitmap for a subject."""
        key = self._make_key(
            subject_type, subject_id, permission, resource_type, tenant_id
        )
        result = await self._client.client.hgetall(key)
        if not result:
            return None

        data = result.get(b"data")
        revision = result.get(b"revision")

        if data is None or revision is None:
            return None

        return (data, int(revision))

    async def set_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
        bitmap_data: bytes,
        revision: int,
    ) -> None:
        """Store Tiger bitmap for a subject."""
        key = self._make_key(
            subject_type, subject_id, permission, resource_type, tenant_id
        )
        pipe = self._client.client.pipeline()
        pipe.hset(key, mapping={"data": bitmap_data, "revision": str(revision)})
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def invalidate(
        self,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        permission: Optional[str] = None,
        resource_type: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> int:
        """Invalidate Tiger cache entries matching criteria."""
        # Build pattern from provided filters
        parts = [
            tenant_id or "*",
            subject_type or "*",
            subject_id or "*",
            permission or "*",
            resource_type or "*",
        ]
        pattern = f"tiger:{':'.join(parts)}"
        return await self._delete_by_pattern(pattern)

    async def _delete_by_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern."""
        deleted = 0
        async for key in self._client.client.scan_iter(match=pattern, count=1000):
            await self._client.client.delete(key)
            deleted += 1
        return deleted

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        return await self._client.health_check()


class DragonflyResourceMapCache:
    """Dragonfly-backed resource map cache.

    Maps resource UUIDs to integer IDs for Roaring Bitmap compatibility.

    Key format:
        resmap:{tenant_id}:{resource_type}

    Value:
        Hash mapping resource_id -> int_id
    """

    def __init__(self, client: DragonflyClient):
        """Initialize resource map cache.

        Args:
            client: DragonflyClient instance
        """
        self._client = client

    def _make_key(self, resource_type: str, tenant_id: str) -> str:
        """Create cache key for resource map."""
        return f"resmap:{tenant_id}:{resource_type}"

    async def get_int_id(
        self,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
    ) -> Optional[int]:
        """Get integer ID for a resource."""
        key = self._make_key(resource_type, tenant_id)
        result = await self._client.client.hget(key, resource_id)
        if result is None:
            return None
        return int(result)

    async def get_int_ids_bulk(
        self,
        resources: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], Optional[int]]:
        """Bulk get integer IDs for multiple resources."""
        if not resources:
            return {}

        # Group by (resource_type, tenant_id) for efficient HMGET
        groups: dict[tuple[str, str], list[str]] = {}
        for resource_type, resource_id, tenant_id in resources:
            group_key = (resource_type, tenant_id)
            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(resource_id)

        results: dict[tuple[str, str, str], Optional[int]] = {}

        for (resource_type, tenant_id), resource_ids in groups.items():
            key = self._make_key(resource_type, tenant_id)
            values = await self._client.client.hmget(key, resource_ids)

            for resource_id, value in zip(resource_ids, values):
                result_key = (resource_type, resource_id, tenant_id)
                results[result_key] = int(value) if value is not None else None

        return results

    async def set_int_id(
        self,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
        int_id: int,
    ) -> None:
        """Store integer ID for a resource."""
        key = self._make_key(resource_type, tenant_id)
        await self._client.client.hset(key, resource_id, str(int_id))

    async def set_int_ids_bulk(
        self,
        mappings: dict[tuple[str, str, str], int],
    ) -> None:
        """Bulk store integer IDs for multiple resources."""
        if not mappings:
            return

        # Group by (resource_type, tenant_id) for efficient HSET
        groups: dict[tuple[str, str], dict[str, str]] = {}
        for (resource_type, resource_id, tenant_id), int_id in mappings.items():
            group_key = (resource_type, tenant_id)
            if group_key not in groups:
                groups[group_key] = {}
            groups[group_key][resource_id] = str(int_id)

        pipe = self._client.client.pipeline()
        for (resource_type, tenant_id), mapping in groups.items():
            key = self._make_key(resource_type, tenant_id)
            pipe.hset(key, mapping=mapping)
        await pipe.execute()
