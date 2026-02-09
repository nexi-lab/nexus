"""In-memory cache backend for dev/test environments.

Provides a pure-Python CacheStoreABC driver with no external dependencies.
Uses dict for KV storage and asyncio for PubSub.

This is NOT the same as NullCacheStore (which is a no-op for kernel-only mode).
InMemoryCacheStore actually stores data — it's a real cache, just not distributed.

OS Analogy: tmpfs backed by page cache (real storage, but process-local).

Usage:
    store = InMemoryCacheStore()

    await store.set("key", b"value", ttl=300)
    data = await store.get("key")

    async with store.subscribe("events") as messages:
        async for msg in messages:
            process(msg)
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from nexus.core.cache_store import CacheStoreABC


class InMemoryCacheStore(CacheStoreABC):
    """In-memory CacheStoreABC driver for dev/test.

    KV: dict with lazy TTL expiration (checked on access).
    PubSub: asyncio.Queue per subscriber.

    Thread Safety: NOT thread-safe. Use only within a single asyncio event loop.
    """

    def __init__(self) -> None:
        # key -> (value, expire_at_monotonic | None)
        self._store: dict[str, tuple[bytes, float | None]] = {}
        # channel -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue[bytes | None]]] = defaultdict(list)
        self._closed = False

    # --- KV operations ---

    async def get(self, key: str) -> bytes | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expire_at = entry
        if expire_at is not None and time.monotonic() > expire_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        expire_at = (time.monotonic() + ttl) if ttl is not None else None
        self._store[key] = (value, expire_at)

    async def delete(self, key: str) -> bool:
        try:
            del self._store[key]
            return True
        except KeyError:
            return False

    async def exists(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        _, expire_at = entry
        if expire_at is not None and time.monotonic() > expire_at:
            del self._store[key]
            return False
        return True

    async def delete_by_prefix(self, prefix: str) -> int:
        to_delete = [k for k in self._store if k.startswith(prefix)]
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
        self._store.clear()
        # Signal all subscribers to stop
        for queues in self._subscribers.values():
            for q in queues:
                q.put_nowait(None)
        self._subscribers.clear()
