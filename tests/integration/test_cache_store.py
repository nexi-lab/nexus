"""Integration tests for CacheStore (Fourth Pillar) — Task #22.

Tests realistic cache workflows using InMemoryCacheStore as the driver.
DragonflyCacheStore would use the same ABC interface against a real Dragonfly instance.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.cache.inmemory import InMemoryCacheStore
from nexus.core.cache_store import CacheStoreABC, NullCacheStore

# ---------------------------------------------------------------------------
# Fixture: shared InMemoryCacheStore for realistic scenarios
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_store():
    """InMemoryCacheStore — real storage, process-local."""
    return InMemoryCacheStore()


# ---------------------------------------------------------------------------
# Scenario 1: Permission caching workflow
# ---------------------------------------------------------------------------


class TestPermissionCachingWorkflow:
    """Simulate how PermissionCache would use CacheStoreABC primitives.

    Real flow: check_permission() → cache.get(key) → miss → evaluate ReBAC → cache.set(key, result, ttl)
    """

    async def test_permission_cache_hit_miss_cycle(self, cache_store):
        """Full cycle: miss → compute → store → hit → invalidate → miss."""
        key = "perm:zone1:user:alice:read:file:/docs/report.md"

        # 1. Cache miss
        assert await cache_store.get(key) is None

        # 2. "Evaluate ReBAC" → grant
        result = b"1"  # granted

        # 3. Store with TTL
        await cache_store.set(key, result, ttl=300)

        # 4. Cache hit
        cached = await cache_store.get(key)
        assert cached == b"1"

        # 5. Permission change → invalidate all permissions for this subject
        deleted = await cache_store.delete_by_prefix("perm:zone1:user:alice:")
        assert deleted == 1

        # 6. Cache miss again
        assert await cache_store.get(key) is None

    async def test_bulk_permission_invalidation(self, cache_store):
        """Invalidate all permissions for a zone (e.g., ACL bulk update)."""
        # Populate multiple permission entries
        keys = [
            "perm:zone1:user:alice:read:file:/a",
            "perm:zone1:user:alice:write:file:/a",
            "perm:zone1:user:bob:read:file:/b",
            "perm:zone2:user:alice:read:file:/c",
        ]
        for k in keys:
            await cache_store.set(k, b"1", ttl=300)

        # Invalidate zone1 only
        deleted = await cache_store.delete_by_prefix("perm:zone1:")
        assert deleted == 3

        # zone2 untouched
        assert await cache_store.get("perm:zone2:user:alice:read:file:/c") == b"1"

    async def test_denial_shorter_ttl_than_grant(self, cache_store):
        """Denials expire faster for security (shorter TTL)."""
        grant_key = "perm:zone1:user:alice:read:file:/public"
        denial_key = "perm:zone1:user:bob:write:file:/secret"

        await cache_store.set(grant_key, b"1", ttl=300)  # 5 min for grants
        await cache_store.set(denial_key, b"0", ttl=60)  # 1 min for denials

        # Both exist
        assert await cache_store.exists(grant_key)
        assert await cache_store.exists(denial_key)


# ---------------------------------------------------------------------------
# Scenario 2: PubSub event distribution
# ---------------------------------------------------------------------------


class TestEventDistribution:
    """Simulate EventBus using CacheStoreABC PubSub primitives."""

    async def test_file_change_event_broadcast(self, cache_store):
        """Publish file change event → all subscribers receive it."""
        received: list[bytes] = []

        async def watcher():
            async with cache_store.subscribe("events:zone1") as messages:
                async for msg in messages:
                    received.append(msg)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(watcher())
        await asyncio.sleep(0.01)  # let subscriber register

        # Simulate two file writes
        event1 = b'{"type":"write","path":"/docs/a.md"}'
        event2 = b'{"type":"write","path":"/docs/b.md"}'

        n1 = await cache_store.publish("events:zone1", event1)
        n2 = await cache_store.publish("events:zone1", event2)

        assert n1 == 1  # one subscriber
        assert n2 == 1

        await asyncio.wait_for(task, timeout=2.0)
        assert received == [event1, event2]

    async def test_cross_zone_isolation(self, cache_store):
        """Events in zone1 don't leak to zone2 subscribers."""
        zone2_received: list[bytes] = []

        async def zone2_watcher():
            async with cache_store.subscribe("events:zone2") as messages:
                async for msg in messages:
                    zone2_received.append(msg)
                    break

        task = asyncio.create_task(zone2_watcher())
        await asyncio.sleep(0.01)

        # Publish to zone1 — zone2 watcher should NOT receive
        await cache_store.publish("events:zone1", b"zone1-event")

        # Publish to zone2 — zone2 watcher SHOULD receive
        await cache_store.publish("events:zone2", b"zone2-event")

        await asyncio.wait_for(task, timeout=2.0)
        assert zone2_received == [b"zone2-event"]


# ---------------------------------------------------------------------------
# Scenario 3: Graceful degrade (NullCacheStore)
# ---------------------------------------------------------------------------


class TestGracefulDegrade:
    """NullCacheStore provides no-op behavior — kernel works without cache."""

    async def test_permission_check_without_cache(self):
        """When no CacheStore: every permission check is a cache miss → direct query."""
        store = NullCacheStore()

        # Always miss — caller falls through to ReBAC direct query
        assert await store.get("perm:zone1:user:alice:read:file:/a") is None

        # Set is a no-op
        await store.set("perm:zone1:user:alice:read:file:/a", b"1", ttl=300)
        assert await store.get("perm:zone1:user:alice:read:file:/a") is None

    async def test_event_publish_without_cache(self):
        """When no CacheStore: publish reaches 0 subscribers (EventBus disabled)."""
        store = NullCacheStore()
        count = await store.publish("events:zone1", b"event")
        assert count == 0

    async def test_null_is_cachestoreABC(self):
        """NullCacheStore satisfies the same interface as real drivers."""
        store = NullCacheStore()
        assert isinstance(store, CacheStoreABC)

    async def test_inmemory_is_cachestoreABC(self):
        """InMemoryCacheStore satisfies the same interface as real drivers."""
        store = InMemoryCacheStore()
        assert isinstance(store, CacheStoreABC)


# ---------------------------------------------------------------------------
# Scenario 4: TTL expiration behavior
# ---------------------------------------------------------------------------


class TestTTLExpiration:
    """TTL-based cache expiration in InMemoryCacheStore."""

    async def test_expired_key_returns_none(self, cache_store):
        """Keys with expired TTL return None on access (lazy eviction)."""
        await cache_store.set("temp", b"data", ttl=0)
        await asyncio.sleep(0.01)
        assert await cache_store.get("temp") is None

    async def test_expired_key_not_in_exists(self, cache_store):
        """Expired keys also return False for exists()."""
        await cache_store.set("temp", b"data", ttl=0)
        await asyncio.sleep(0.01)
        assert await cache_store.exists("temp") is False

    async def test_no_ttl_persists_indefinitely(self, cache_store):
        """Keys without TTL never expire."""
        await cache_store.set("permanent", b"data")
        await asyncio.sleep(0.05)
        assert await cache_store.get("permanent") == b"data"
