"""In-memory cache backend for dev/test environments.

Provides a pure-Python CacheStoreABC driver with no external dependencies.
Uses OrderedDict for KV storage with optional LRU eviction and asyncio for PubSub.

This is NOT the same as NullCacheStore (which is a no-op for kernel-only mode).
InMemoryCacheStore actually stores data — it's a real cache, just not distributed.

OS Analogy: tmpfs backed by page cache (real storage, but process-local).

Usage:
    store = InMemoryCacheStore()

    await store.set("key", b"value", ttl=300)
    data = await store.get("key")

    # With LRU eviction (Decision #15)
    store = InMemoryCacheStore(max_size=10000)
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fnmatch import fnmatch

from nexus.core.protocols import CacheStoreABC


class InMemoryCacheStore(CacheStoreABC):
    """In-memory CacheStoreABC driver for dev/test.

    KV: OrderedDict with lazy TTL expiration and optional LRU eviction.
    PubSub: asyncio.Queue per subscriber.

    Args:
        max_size: Maximum number of entries. 0 = unlimited (default).
            When the limit is hit, the least-recently-used entry is evicted.

    Thread Safety: Uses threading.Lock for LRU eviction safety.
    """

    def __init__(self, max_size: int = 0) -> None:
        # key -> (value, expire_at_monotonic | None)
        self._store: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()
        # channel -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue[bytes | None]]] = defaultdict(list)
        self._closed = False
        self._max_size = max_size
        self._evictions = 0
        self._lock = threading.Lock()

    # --- KV operations ---

    async def get(self, key: str) -> bytes | None:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            try:
                del self._store[key]
                return True
            except KeyError:
                return False

    async def exists(self, key: str) -> bool:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            _, expire_at = entry
            if expire_at is not None and time.monotonic() > expire_at:
                del self._store[key]
                return False
            return True

    async def delete_by_pattern(self, pattern: str) -> int:
        with self._lock:
            to_delete = [k for k in self._store if fnmatch(k, pattern)]
            for k in to_delete:
                del self._store[k]
            return len(to_delete)

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
        with self._lock:
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
        with self._lock:
            return {
                "current_size": len(self._store),
                "max_size": self._max_size,
                "evictions": self._evictions,
            }
