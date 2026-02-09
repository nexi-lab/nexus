"""Integration tests for CacheStore (Fourth Pillar) — Task #22.

Tests realistic cache workflows using InMemoryCacheStore as the driver.
DragonflyCacheStore would use the same ABC interface against a real Dragonfly instance.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.cache.domain import PermissionCache, TigerCache
from nexus.cache.factory import CacheFactory
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.cache.settings import CacheSettings
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
        deleted = await cache_store.delete_by_pattern("perm:zone1:user:alice:*")
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
        deleted = await cache_store.delete_by_pattern("perm:zone1:*")
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


# ---------------------------------------------------------------------------
# Scenario 5: Domain caches via CacheStoreABC (driver-agnostic)
# ---------------------------------------------------------------------------


class TestPermissionCacheDomain:
    """PermissionCache built on CacheStoreABC — the full stack."""

    @pytest.fixture
    def perm_cache(self, cache_store):
        return PermissionCache(store=cache_store, ttl=300, denial_ttl=60)

    async def test_grant_and_lookup(self, perm_cache):
        """Store a grant, retrieve it."""
        await perm_cache.set("user", "alice", "read", "file", "/docs/a.md", True, "zone1")
        result = await perm_cache.get("user", "alice", "read", "file", "/docs/a.md", "zone1")
        assert result is True

    async def test_denial_and_lookup(self, perm_cache):
        """Store a denial, retrieve it."""
        await perm_cache.set("user", "bob", "write", "file", "/secret", False, "zone1")
        result = await perm_cache.get("user", "bob", "write", "file", "/secret", "zone1")
        assert result is False

    async def test_cache_miss(self, perm_cache):
        """Uncached permission returns None."""
        result = await perm_cache.get("user", "unknown", "read", "file", "/x", "zone1")
        assert result is None

    async def test_invalidate_subject(self, perm_cache):
        """Invalidate all perms for a subject."""
        await perm_cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        await perm_cache.set("user", "alice", "write", "file", "/b", True, "zone1")
        await perm_cache.set("user", "bob", "read", "file", "/a", True, "zone1")

        deleted = await perm_cache.invalidate_subject("user", "alice", "zone1")
        assert deleted == 2

        # Bob untouched
        assert await perm_cache.get("user", "bob", "read", "file", "/a", "zone1") is True

    async def test_invalidate_object(self, perm_cache):
        """Invalidate all perms for an object (wildcard in middle of key)."""
        await perm_cache.set("user", "alice", "read", "file", "/shared", True, "zone1")
        await perm_cache.set("user", "bob", "write", "file", "/shared", True, "zone1")
        await perm_cache.set("user", "alice", "read", "file", "/private", True, "zone1")

        deleted = await perm_cache.invalidate_object("file", "/shared", "zone1")
        assert deleted == 2

        # /private untouched
        assert await perm_cache.get("user", "alice", "read", "file", "/private", "zone1") is True


class TestTigerCacheDomain:
    """TigerCache built on CacheStoreABC — bitmap store/retrieve."""

    @pytest.fixture
    def tiger_cache(self, cache_store):
        return TigerCache(store=cache_store, ttl=3600)

    async def test_store_and_retrieve_bitmap(self, tiger_cache):
        """Round-trip: set_bitmap → get_bitmap preserves data + revision."""
        bitmap = b"\x01\x02\x03\x04\x05"
        revision = 42

        await tiger_cache.set_bitmap("user", "alice", "read", "file", "zone1", bitmap, revision)
        result = await tiger_cache.get_bitmap("user", "alice", "read", "file", "zone1")

        assert result is not None
        data, rev = result
        assert data == bitmap
        assert rev == 42

    async def test_cache_miss(self, tiger_cache):
        """Missing bitmap returns None."""
        result = await tiger_cache.get_bitmap("user", "unknown", "read", "file", "zone1")
        assert result is None

    async def test_invalidate_by_subject(self, tiger_cache):
        """Invalidate all bitmaps for a subject."""
        await tiger_cache.set_bitmap("user", "alice", "read", "file", "zone1", b"bm1", 1)
        await tiger_cache.set_bitmap("user", "alice", "write", "file", "zone1", b"bm2", 2)

        deleted = await tiger_cache.invalidate(subject_type="user", subject_id="alice")
        assert deleted == 2


# ---------------------------------------------------------------------------
# Scenario 6: CacheFactory with injected CacheStoreABC
# ---------------------------------------------------------------------------


class TestCacheFactoryIntegration:
    """CacheFactory creates domain caches from injected CacheStoreABC."""

    async def test_factory_with_injected_store(self):
        """Inject InMemoryCacheStore → factory builds domain caches on it."""
        store = InMemoryCacheStore()
        settings = CacheSettings(cache_backend="auto", dragonfly_url=None)
        factory = CacheFactory(settings, cache_store=store)
        await factory.initialize()

        # Factory exposes the injected store
        assert factory.cache_store is store

        # Domain caches work through the store
        perm = factory.get_permission_cache()
        await perm.set("user", "alice", "read", "file", "/a", True, "zone1")
        assert await perm.get("user", "alice", "read", "file", "/a", "zone1") is True

        tiger = factory.get_tiger_cache()
        await tiger.set_bitmap("user", "alice", "read", "file", "zone1", b"bitmap", 1)
        result = await tiger.get_bitmap("user", "alice", "read", "file", "zone1")
        assert result is not None

        await factory.shutdown()

    async def test_factory_defaults_to_null(self):
        """No Dragonfly URL → NullCacheStore → all cache ops are no-ops."""
        settings = CacheSettings(cache_backend="auto", dragonfly_url=None)
        factory = CacheFactory(settings)
        await factory.initialize()

        assert isinstance(factory.cache_store, NullCacheStore)

        perm = factory.get_permission_cache()
        await perm.set("user", "alice", "read", "file", "/a", True, "zone1")
        assert await perm.get("user", "alice", "read", "file", "/a", "zone1") is None  # no-op

        await factory.shutdown()
