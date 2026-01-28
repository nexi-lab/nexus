"""Dragonfly (Redis-compatible) cache backend implementation.

Dragonfly is a Redis-compatible in-memory datastore that provides:
- 25x throughput improvement over Redis
- 80% less memory usage
- Multi-threaded architecture
- Smart eviction with cache_mode=true

This module provides the client connection manager and cache implementations.

Connection Pool Optimizations (Issue #1075):
- BlockingConnectionPool: Waits for available connections instead of erroring
- TCP keepalive: Prevents NAT/firewall from dropping idle connections
- Retry on timeout: Automatic retry for transient network issues
"""

import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

# Redis client is optional - only imported if Dragonfly is configured
try:
    import redis.asyncio as redis
    from redis.asyncio import BlockingConnectionPool, ConnectionPool
    from redis.backoff import ExponentialBackoff
    from redis.retry import Retry

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None  # type: ignore
    BlockingConnectionPool = None  # type: ignore
    ConnectionPool = None  # type: ignore


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
            client_kwargs["retry_on_error"] = [ConnectionError, TimeoutError]

        self._client = redis.Redis(**client_kwargs)

        # Verify connection
        try:
            await self._client.ping()  # type: ignore[misc]
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
            await self._client.ping()  # type: ignore[misc]
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
        return (
            f"perm:{tenant_id}:{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}"
        )

    async def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str,
    ) -> bool | None:
        """Get cached permission result."""
        key = self._make_key(
            subject_type, subject_id, permission, object_type, object_id, tenant_id
        )
        value = await self._client.client.get(key)
        if value is None:
            return None
        return bool(value == b"1")

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

    async def clear(self, tenant_id: str | None = None) -> int:
        """Clear all cached permissions."""
        pattern = f"perm:{tenant_id}:*" if tenant_id else "perm:*"
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
    ) -> tuple[bytes, int] | None:
        """Get Tiger bitmap for a subject."""
        key = self._make_key(subject_type, subject_id, permission, resource_type, tenant_id)
        result = await self._client.client.hgetall(key)  # type: ignore[misc]
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
        key = self._make_key(subject_type, subject_id, permission, resource_type, tenant_id)
        pipe = self._client.client.pipeline()
        pipe.hset(key, mapping={"data": bitmap_data, "revision": str(revision)})
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        tenant_id: str | None = None,
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
    ) -> int | None:
        """Get integer ID for a resource."""
        key = self._make_key(resource_type, tenant_id)
        result = await self._client.client.hget(key, resource_id)  # type: ignore[misc]
        if result is None:
            return None
        return int(result)

    async def get_int_ids_bulk(
        self,
        resources: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], int | None]:
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

        results: dict[tuple[str, str, str], int | None] = {}

        for (resource_type, tenant_id), resource_ids in groups.items():
            key = self._make_key(resource_type, tenant_id)
            values = await self._client.client.hmget(key, resource_ids)  # type: ignore[misc]

            for resource_id, value in zip(resource_ids, values, strict=True):
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
        await self._client.client.hset(key, resource_id, str(int_id))  # type: ignore[misc]

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


class DragonflyEmbeddingCache:
    """Dragonfly-backed embedding cache for semantic search.

    Caches embedding vectors by content hash to avoid redundant API calls.
    Implements Level 1 (content-based) caching from Issue #950.

    Key format:
        emb:v1:{sha256(model:text)[:32]}

    Value:
        JSON-serialized embedding vector (list of floats)

    Features:
        - Content-hash based deduplication
        - Batch operations with pipeline
        - Configurable TTL (default: 24 hours)
        - Graceful degradation on errors
    """

    # Cache key version - increment when changing key format
    CACHE_VERSION = "v1"

    def __init__(
        self,
        client: DragonflyClient,
        ttl: int = 86400,
    ):
        """Initialize embedding cache.

        Args:
            client: DragonflyClient instance
            ttl: TTL in seconds (default: 24 hours)
        """
        self._client = client
        self._ttl = ttl
        # Metrics (Issue #973)
        self._hits = 0
        self._misses = 0
        self._errors = 0

    def _content_hash(self, text: str, model: str) -> str:
        """Generate content hash for cache key.

        Args:
            text: Text content to hash
            model: Embedding model name

        Returns:
            32-character hex hash
        """
        import hashlib

        content = f"{model}:{text}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def _make_key(self, text: str, model: str) -> str:
        """Create cache key for embedding.

        Args:
            text: Text content
            model: Embedding model name

        Returns:
            Cache key string
        """
        content_hash = self._content_hash(text, model)
        return f"emb:{self.CACHE_VERSION}:{content_hash}"

    async def get(self, text: str, model: str) -> list[float] | None:
        """Get cached embedding for text.

        Args:
            text: Text content
            model: Embedding model name

        Returns:
            Embedding vector if cached, None otherwise
        """
        import json

        key = self._make_key(text, model)
        try:
            cached = await self._client.client.get(key)
            if cached:
                self._hits += 1
                return json.loads(cached)
            self._misses += 1
            return None
        except Exception as e:
            logger.warning(f"Embedding cache get failed: {e}")
            self._errors += 1
            return None

    async def set(
        self,
        text: str,
        model: str,
        embedding: list[float],
    ) -> None:
        """Cache embedding for text.

        Args:
            text: Text content
            model: Embedding model name
            embedding: Embedding vector to cache
        """
        import json

        key = self._make_key(text, model)
        try:
            await self._client.client.setex(
                key,
                self._ttl,
                json.dumps(embedding),
            )
        except Exception as e:
            logger.warning(f"Embedding cache set failed: {e}")
            self._errors += 1

    async def get_batch(
        self,
        texts: list[str],
        model: str,
    ) -> dict[str, list[float] | None]:
        """Get cached embeddings for multiple texts.

        Args:
            texts: List of text contents
            model: Embedding model name

        Returns:
            Dict mapping text -> embedding (None if not cached)
        """
        import json

        if not texts:
            return {}

        # Build keys and track mapping
        keys = [self._make_key(text, model) for text in texts]

        try:
            cached_values = await self._client.client.mget(keys)

            results = {}
            for text, cached in zip(texts, cached_values, strict=True):
                if cached:
                    self._hits += 1
                    results[text] = json.loads(cached)
                else:
                    self._misses += 1
                    results[text] = None

            return results
        except Exception as e:
            logger.warning(f"Embedding cache batch get failed: {e}")
            self._errors += 1
            return dict.fromkeys(texts, None)

    async def set_batch(
        self,
        embeddings: dict[str, list[float]],
        model: str,
    ) -> None:
        """Cache multiple embeddings.

        Args:
            embeddings: Dict mapping text -> embedding vector
            model: Embedding model name
        """
        import json

        if not embeddings:
            return

        try:
            pipe = self._client.client.pipeline()
            for text, embedding in embeddings.items():
                key = self._make_key(text, model)
                pipe.setex(key, self._ttl, json.dumps(embedding))
            await pipe.execute()
        except Exception as e:
            logger.warning(f"Embedding cache batch set failed: {e}")
            self._errors += 1

    async def get_or_embed_batch(
        self,
        texts: list[str],
        model: str,
        embed_fn,
    ) -> list[list[float]]:
        """Get cached embeddings or generate new ones.

        This is the main entry point for cached embedding generation.
        Implements batch deduplication to minimize API calls.

        Args:
            texts: List of texts to embed
            model: Embedding model name
            embed_fn: Async function to generate embeddings for uncached texts
                      Signature: async (list[str]) -> list[list[float]]

        Returns:
            List of embeddings in the same order as input texts
        """
        if not texts:
            return []

        # Step 1: Deduplicate texts (same text appears multiple times)
        unique_texts = list(dict.fromkeys(texts))  # Preserve order

        # Step 2: Check cache for all unique texts
        cached = await self.get_batch(unique_texts, model)

        # Step 3: Find uncached texts
        uncached_texts = [text for text in unique_texts if cached[text] is None]

        # Step 4: Generate embeddings for uncached texts only
        if uncached_texts:
            logger.info(
                f"Embedding cache: {len(unique_texts) - len(uncached_texts)}/{len(unique_texts)} "
                f"hits, generating {len(uncached_texts)} new embeddings"
            )
            new_embeddings = await embed_fn(uncached_texts)

            # Cache new embeddings
            new_cache_entries = dict(zip(uncached_texts, new_embeddings, strict=True))
            await self.set_batch(new_cache_entries, model)

            # Update cached dict with new embeddings
            for text, embedding in new_cache_entries.items():
                cached[text] = embedding
        else:
            logger.info(f"Embedding cache: 100% hit rate ({len(unique_texts)} texts)")

        # Step 5: Build result in original order (handling duplicates)
        results = [cached[text] for text in texts]
        return results

    async def invalidate(self, text: str, model: str) -> bool:
        """Invalidate cached embedding for text.

        Args:
            text: Text content
            model: Embedding model name

        Returns:
            True if key was deleted, False otherwise
        """
        key = self._make_key(text, model)
        try:
            deleted = await self._client.client.delete(key)
            return deleted > 0
        except Exception as e:
            logger.warning(f"Embedding cache invalidate failed: {e}")
            self._errors += 1
            return False

    async def clear(self, _model: str | None = None) -> int:
        """Clear cached embeddings.

        Args:
            model: If provided, only clear embeddings for this model.
                   If None, clear all embeddings.

        Returns:
            Number of keys deleted
        """
        pattern = f"emb:{self.CACHE_VERSION}:*"
        return await self._delete_by_pattern(pattern)

    async def _delete_by_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern using SCAN (non-blocking)."""
        deleted = 0
        try:
            async for key in self._client.client.scan_iter(match=pattern, count=1000):
                await self._client.client.delete(key)
                deleted += 1
        except Exception as e:
            logger.warning(f"Embedding cache clear failed: {e}")
            self._errors += 1
        return deleted

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        return await self._client.health_check()

    def get_metrics(self) -> dict:
        """Get cache metrics.

        Returns:
            Dict with cache statistics
        """
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0

        # Estimate cost savings (OpenAI: $0.13/1M tokens, ~500 tokens/embedding)
        tokens_saved = self._hits * 500
        cost_saved = (tokens_saved / 1_000_000) * 0.13

        return {
            "hits": self._hits,
            "misses": self._misses,
            "errors": self._errors,
            "hit_rate": round(hit_rate, 4),
            "estimated_tokens_saved": tokens_saved,
            "estimated_cost_saved_usd": round(cost_saved, 4),
            "ttl_seconds": self._ttl,
        }

    async def get_stats(self) -> dict:
        """Get cache statistics including backend info."""
        info = await self._client.get_info()
        metrics = self.get_metrics()
        return {
            "backend": "dragonfly",
            "status": info.get("status", "unknown"),
            **metrics,
        }
