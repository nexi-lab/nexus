"""CacheStore: The "Ephemeral" pillar of the Nexus Quartet.

Provides ephemeral KV + PubSub storage for hot caching and event distribution.
This is one of the Four Pillars (Metastore, RecordStore, ObjectStore, CacheStore).

OS Analogy: /dev/shm + D-Bus (shared memory for hot data + message bus).
Backing Tech: Dragonfly (production) / In-Memory dict (dev/test).

CacheStore is NOT required by the Kernel. When absent, consumers degrade gracefully:
- EventBus: disabled (distributed-only feature, single-node doesn't need it)
- PermissionCache: direct-queries RecordStore (correct, slower)
- TigerCache: O(n) permission checks (no pre-materialized bitmaps)
- UserSession: stays in RecordStore (acceptable for kernel-only)

Usage:
    # Production (Dragonfly)
    cache_store = DragonflyCacheStore(url="redis://localhost:6379")

    # Dev/test (In-Memory)
    cache_store = InMemoryCacheStore()

    # Kernel init (optional — only needed when caching/events are desired)
    nx = NexusFS(backend=backend, metadata_store=store, cache_store=cache_store)

    # Kernel-only (no cache — graceful degrade)
    nx = NexusFS(backend=backend, metadata_store=store)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class CacheStoreABC(ABC):
    """Abstract base class for ephemeral KV + PubSub storage (the "Ephemeral" pillar).

    Provides low-level primitives that domain caches are built upon:
    - KV: get/set/delete with optional TTL
    - PubSub: publish/subscribe on named channels

    Domain protocols (PermissionCacheProtocol, TigerCacheProtocol, EventBusProtocol)
    are consumer-level interfaces built ON TOP of these primitives.

    Implementations:
    - DragonflyCacheStore: Production driver (Redis-compatible, external process)
    - InMemoryCacheStore: Dev/test driver (dict + asyncio.Queue, no external deps)
    """

    # --- KV operations ---

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """Get value by key.

        Returns None if key does not exist or has expired.
        """
        ...

    @abstractmethod
    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Set key to value with optional TTL in seconds.

        If key already exists, it is overwritten.
        If ttl is None, the key does not expire.
        """
        ...

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if the key existed."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if a key exists (and has not expired)."""
        ...

    @abstractmethod
    async def delete_by_pattern(self, pattern: str) -> int:
        """Delete all keys matching a glob pattern. Returns count of deleted keys.

        Supports ``*`` as wildcard. Examples:
        - ``perm:zone1:*`` — all zone1 permission keys (prefix match)
        - ``perm:*:user:alice:*`` — all permission keys for alice across zones

        Used for targeted cache invalidation.
        """
        ...

    # --- Batch KV operations ---

    async def get_many(self, keys: list[str]) -> dict[str, bytes | None]:
        """Get multiple keys in one call. Returns {key: value_or_None}.

        Default: sequential get() calls. Drivers SHOULD override with
        pipeline/MGET for fewer round-trips when batch performance matters.
        """
        return {k: await self.get(k) for k in keys}

    async def set_many(
        self, mapping: dict[str, bytes], ttl: int | None = None
    ) -> None:
        """Set multiple keys in one call.

        Default: sequential set() calls. Drivers SHOULD override with
        pipeline/MSET for fewer round-trips when batch performance matters.
        """
        for k, v in mapping.items():
            await self.set(k, v, ttl=ttl)

    # --- PubSub operations ---

    @abstractmethod
    async def publish(self, channel: str, message: bytes) -> int:
        """Publish a message to a channel. Returns number of receivers."""
        ...

    @abstractmethod
    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """Subscribe to a channel. Yields an async iterator of messages.

        Usage:
            async with store.subscribe("events:zone1") as messages:
                async for msg in messages:
                    process(msg)
        """
        ...  # pragma: no cover
        yield  # type: ignore[misc]

    # --- Lifecycle ---

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the cache backend is healthy and responding."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    async def __aenter__(self) -> CacheStoreABC:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


class NullCacheStore(CacheStoreABC):
    """No-op CacheStore — the fscache CONFIG_FSCACHE=n equivalent.

    Default when no cache driver is injected. All operations are no-ops:
    - KV get/exists → None/False (cache miss)
    - KV set/delete → silently ignored
    - PubSub publish → 0 receivers
    - PubSub subscribe → empty stream

    Kernel code never checks for None — it always talks to CacheStoreABC.
    NullCacheStore makes "no cache" invisible to the kernel.
    """

    async def get(self, key: str) -> bytes | None:
        return None

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        pass

    async def delete(self, key: str) -> bool:
        return False

    async def exists(self, key: str) -> bool:
        return False

    async def delete_by_pattern(self, pattern: str) -> int:
        return 0

    async def publish(self, channel: str, message: bytes) -> int:
        return 0

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[bytes]]:
        async def _empty() -> AsyncIterator[bytes]:
            return
            yield  # make it an async generator

        yield _empty()

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass
