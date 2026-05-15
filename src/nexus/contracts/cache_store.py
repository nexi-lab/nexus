"""CacheStoreABC — the "Ephemeral" pillar of the Nexus Quartet.

Provides ephemeral KV + PubSub storage for hot caching and event distribution.
This is one of the Four Pillars (Metastore, RecordStore, ObjectStore, CacheStore).

OS Analogy: /dev/shm + D-Bus (shared memory for hot data + message bus).
Backing Tech: Dragonfly (production) / In-Memory dict (dev/test).

CacheStore is NOT required by the Kernel. When absent, consumers degrade gracefully:
- EventBus: disabled (distributed-only feature, single-node doesn't need it)
- PermissionCache: direct-queries RecordStore (correct, slower)
- TigerCache: O(n) permission checks (no pre-materialized bitmaps)
- UserSession: session management unavailable (CacheStore required)

Canonical home for CacheStoreABC (Issue #2055). Lives in contracts/ because it is
a multi-layer type used by kernel, services, and bricks — per §3.1 Placement
Decision Tree: "Multi-layer types → contracts/".

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

import asyncio
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from fnmatch import fnmatch
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

    @abstractmethod
    async def keys_by_pattern(self, pattern: str) -> list[str]:
        """Return all keys matching a glob pattern.

        Supports ``*`` as wildcard. Examples:
        - ``session:zone1:*`` — all zone1 session keys
        - ``perm:*:user:alice:*`` — permission keys for alice across zones

        Companion to ``delete_by_pattern`` — same pattern syntax, but returns
        the matching key names instead of deleting them.  Enables enumeration
        of cached entries for listing/filtering operations.

        Drivers:
        - InMemoryCacheStore: ``fnmatch`` over dict keys (same as delete_by_pattern)
        - DragonflyCacheStore: ``SCAN`` cursor (same as delete_by_pattern)
        - NullCacheStore: always returns ``[]``
        """
        ...

    # --- Batch KV operations ---

    async def get_many(self, keys: list[str]) -> dict[str, bytes | None]:
        """Get multiple keys in one call. Returns {key: value_or_None}.

        Default: sequential get() calls. Drivers SHOULD override with
        pipeline/MGET for fewer round-trips when batch performance matters.
        """
        return {k: await self.get(k) for k in keys}

    async def set_many(self, mapping: dict[str, bytes], ttl: int | None = None) -> None:
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
    def subscribe(self, channel: str) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """Subscribe to a channel. Returns an async context manager yielding messages.

        Usage:
            async with store.subscribe("events:zone1") as messages:
                async for msg in messages:
                    process(msg)
        """
        ...

    # --- Lifecycle ---

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the cache backend is healthy and responding."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    async def __aenter__(self) -> "CacheStoreABC":
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

    async def get(self, key: str) -> bytes | None:  # noqa: ARG002
        return None

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:  # noqa: ARG002
        pass

    async def delete(self, key: str) -> bool:  # noqa: ARG002
        return False

    async def exists(self, key: str) -> bool:  # noqa: ARG002
        return False

    async def delete_by_pattern(self, pattern: str) -> int:  # noqa: ARG002
        return 0

    async def keys_by_pattern(self, pattern: str) -> list[str]:  # noqa: ARG002
        return []

    async def publish(self, _channel: str, _message: bytes) -> int:
        return 0

    @asynccontextmanager
    async def subscribe(self, _channel: str) -> AsyncIterator[AsyncIterator[bytes]]:
        async def _empty() -> AsyncIterator[bytes]:
            return
            yield  # make it an async generator

        yield _empty()

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class InMemoryCacheStore(CacheStoreABC):
    """In-memory CacheStoreABC driver for dev/test.

    KV: OrderedDict with lazy TTL expiration and optional LRU eviction.
    PubSub: asyncio.Queue per subscriber.

    OS Analogy: tmpfs backed by page cache (real storage, but process-local).

    Args:
        max_size: Maximum number of entries. 0 = unlimited (default).
            When the limit is hit, the least-recently-used entry is evicted.

    Thread Safety: Uses asyncio.Lock for cooperative async safety (Issue #3063).
    """

    def __init__(self, max_size: int = 0) -> None:
        # key -> (value, expire_at_monotonic | None)
        self._store: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()
        # channel -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue[bytes | None]]] = defaultdict(list)
        self._closed = False
        self._max_size = max_size
        self._evictions = 0
        self._lock = asyncio.Lock()

    # --- KV operations ---

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expire_at = entry
            if expire_at is not None and time.monotonic() > expire_at:
                del self._store[key]
                return None
            # Refresh LRU position on access
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        expire_at = (time.monotonic() + ttl) if ttl is not None else None
        async with self._lock:
            if key in self._store:
                # Update existing — refresh LRU
                self._store[key] = (value, expire_at)
                self._store.move_to_end(key)
            else:
                # New entry — check capacity
                if self._max_size > 0:
                    self._evict_if_needed()
                self._store[key] = (value, expire_at)

    async def delete(self, key: str) -> bool:
        async with self._lock:
            try:
                del self._store[key]
                return True
            except KeyError:
                return False

    async def exists(self, key: str) -> bool:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            _, expire_at = entry
            if expire_at is not None and time.monotonic() > expire_at:
                del self._store[key]
                return False
            return True

    async def delete_by_pattern(self, pattern: str) -> int:
        async with self._lock:
            to_delete = [k for k in self._store if fnmatch(k, pattern)]
            for k in to_delete:
                del self._store[k]
            return len(to_delete)

    async def keys_by_pattern(self, pattern: str) -> list[str]:
        now = time.monotonic()
        async with self._lock:
            return [
                k
                for k, (_, expire_at) in self._store.items()
                if fnmatch(k, pattern) and (expire_at is None or now <= expire_at)
            ]

    # --- PubSub operations ---

    async def publish(self, channel: str, message: bytes) -> int:
        queues = self._subscribers.get(channel, [])
        for q in queues:
            q.put_nowait(message)
        return len(queues)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[bytes]]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._subscribers[channel].append(queue)
        try:

            async def _messages() -> AsyncIterator[bytes]:
                while True:
                    msg = await queue.get()
                    if msg is None:
                        break
                    yield msg

            yield _messages()
        finally:
            self._subscribers[channel].remove(queue)
            # Signal sentinel so any blocked reader exits
            queue.put_nowait(None)

    # --- Lifecycle ---

    async def health_check(self) -> bool:
        return not self._closed

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            self._store.clear()
        # Signal all subscribers to stop
        for queues in self._subscribers.values():
            for q in queues:
                q.put_nowait(None)
        self._subscribers.clear()

    # --- LRU eviction (Decision #15) ---

    def _evict_if_needed(self) -> None:
        """Evict least-recently-used entries until under max_size.

        Must be called while holding self._lock.
        First purges expired entries, then evicts LRU.
        """
        if self._max_size <= 0:
            return

        # Purge expired entries first (free slots without counting as eviction)
        now = time.monotonic()
        expired_keys = [k for k, (_, exp) in self._store.items() if exp is not None and now > exp]
        for k in expired_keys:
            del self._store[k]

        # Evict LRU entries until under capacity
        while len(self._store) >= self._max_size:
            self._store.popitem(last=False)  # Pop oldest (LRU)
            self._evictions += 1

    def get_stats(self) -> dict:
        """Get store statistics including eviction count.

        Returns:
            Dict with current_size, max_size, and evictions count.
        """
        # No lock needed — read-only snapshot of atomic int/len values.
        return {
            "current_size": len(self._store),
            "max_size": self._max_size,
            "evictions": self._evictions,
        }
