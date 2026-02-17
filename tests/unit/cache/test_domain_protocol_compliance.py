"""Protocol compliance tests for domain cache implementations.

Verifies that each domain cache class structurally satisfies its Protocol
and exercises core methods using InMemoryCacheStore.
"""

import pytest

from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.domain import (
    EmbeddingCache,
    PermissionCache,
    ResourceMapCache,
    TigerCache,
)
from nexus.cache.inmemory import InMemoryCacheStore


@pytest.fixture
def store() -> InMemoryCacheStore:
    return InMemoryCacheStore()


# ---------------------------------------------------------------------------
# Structural subtyping checks (isinstance with @runtime_checkable)
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    """Verify domain caches are recognized as implementing their protocols."""

    def test_permission_cache_is_protocol(self, store: InMemoryCacheStore) -> None:
        cache = PermissionCache(store)
        assert isinstance(cache, PermissionCacheProtocol)

    def test_tiger_cache_is_protocol(self, store: InMemoryCacheStore) -> None:
        cache = TigerCache(store)
        assert isinstance(cache, TigerCacheProtocol)

    def test_resource_map_cache_is_protocol(self, store: InMemoryCacheStore) -> None:
        cache = ResourceMapCache(store)
        assert isinstance(cache, ResourceMapCacheProtocol)

    def test_embedding_cache_is_protocol(self, store: InMemoryCacheStore) -> None:
        cache = EmbeddingCache(store)
        assert isinstance(cache, EmbeddingCacheProtocol)


# ---------------------------------------------------------------------------
# PermissionCache functional tests
# ---------------------------------------------------------------------------


class TestPermissionCache:
    """Exercise PermissionCache methods with InMemoryCacheStore."""

    @pytest.fixture
    def cache(self, store: InMemoryCacheStore) -> PermissionCache:
        return PermissionCache(store, ttl=300, denial_ttl=60)

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache: PermissionCache) -> None:
        result = await cache.get("user", "alice", "read", "file", "/a.txt", "zone1")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_grant(self, cache: PermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        result = await cache.get("user", "alice", "read", "file", "/a.txt", "zone1")
        assert result is True

    @pytest.mark.asyncio
    async def test_set_and_get_denial(self, cache: PermissionCache) -> None:
        await cache.set("user", "bob", "write", "file", "/b.txt", False, "zone1")
        result = await cache.get("user", "bob", "write", "file", "/b.txt", "zone1")
        assert result is False

    @pytest.mark.asyncio
    async def test_invalidate_subject(self, cache: PermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        await cache.set("user", "alice", "write", "file", "/b.txt", True, "zone1")
        count = await cache.invalidate_subject("user", "alice", "zone1")
        assert count == 2
        assert await cache.get("user", "alice", "read", "file", "/a.txt", "zone1") is None

    @pytest.mark.asyncio
    async def test_invalidate_object(self, cache: PermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/a.txt", True, "zone1")
        count = await cache.invalidate_object("file", "/a.txt", "zone1")
        assert count == 2

    @pytest.mark.asyncio
    async def test_invalidate_subject_object(self, cache: PermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        await cache.set("user", "alice", "write", "file", "/a.txt", True, "zone1")
        count = await cache.invalidate_subject_object("user", "alice", "file", "/a.txt", "zone1")
        assert count == 2

    @pytest.mark.asyncio
    async def test_clear_by_zone(self, cache: PermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/b.txt", True, "zone2")
        count = await cache.clear("zone1")
        assert count == 1
        # zone2 entry still exists
        assert await cache.get("user", "bob", "read", "file", "/b.txt", "zone2") is True

    @pytest.mark.asyncio
    async def test_clear_all(self, cache: PermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/b.txt", True, "zone2")
        count = await cache.clear()
        assert count == 2

    @pytest.mark.asyncio
    async def test_health_check(self, cache: PermissionCache) -> None:
        assert await cache.health_check() is True

    @pytest.mark.asyncio
    async def test_get_stats(self, cache: PermissionCache) -> None:
        stats = await cache.get_stats()
        assert stats["backend"] == "InMemoryCacheStore"
        assert stats["ttl_grants"] == 300
        assert stats["ttl_denials"] == 60

    @pytest.mark.asyncio
    async def test_zone_isolation(self, cache: PermissionCache) -> None:
        """Entries in different zones don't interfere."""
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "zone1")
        await cache.set("user", "alice", "read", "file", "/a.txt", False, "zone2")
        assert await cache.get("user", "alice", "read", "file", "/a.txt", "zone1") is True
        assert await cache.get("user", "alice", "read", "file", "/a.txt", "zone2") is False


# ---------------------------------------------------------------------------
# TigerCache functional tests
# ---------------------------------------------------------------------------


class TestTigerCache:
    """Exercise TigerCache methods with InMemoryCacheStore."""

    @pytest.fixture
    def cache(self, store: InMemoryCacheStore) -> TigerCache:
        return TigerCache(store, ttl=3600)

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache: TigerCache) -> None:
        result = await cache.get_bitmap("user", "alice", "read", "file", "zone1")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_bitmap(self, cache: TigerCache) -> None:
        bitmap_data = b"\x01\x02\x03\x04"
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", bitmap_data, 42)
        result = await cache.get_bitmap("user", "alice", "read", "file", "zone1")
        assert result is not None
        data, revision = result
        assert data == bitmap_data
        assert revision == 42

    @pytest.mark.asyncio
    async def test_revision_encoding(self, cache: TigerCache) -> None:
        """Verify revision is correctly packed/unpacked as big-endian uint32."""
        bitmap_data = b"\xDE\xAD"
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", bitmap_data, 65535)
        result = await cache.get_bitmap("user", "alice", "read", "file", "zone1")
        assert result is not None
        assert result[1] == 65535

    @pytest.mark.asyncio
    async def test_invalidate_all(self, cache: TigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"\x01", 1)
        await cache.set_bitmap("user", "bob", "write", "file", "zone1", b"\x02", 2)
        count = await cache.invalidate()
        assert count == 2

    @pytest.mark.asyncio
    async def test_invalidate_by_zone(self, cache: TigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"\x01", 1)
        await cache.set_bitmap("user", "bob", "read", "file", "zone2", b"\x02", 2)
        count = await cache.invalidate(zone_id="zone1")
        assert count == 1

    @pytest.mark.asyncio
    async def test_health_check(self, cache: TigerCache) -> None:
        assert await cache.health_check() is True


# ---------------------------------------------------------------------------
# ResourceMapCache functional tests
# ---------------------------------------------------------------------------


class TestResourceMapCache:
    """Exercise ResourceMapCache methods with InMemoryCacheStore."""

    @pytest.fixture
    def cache(self, store: InMemoryCacheStore) -> ResourceMapCache:
        return ResourceMapCache(store)

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache: ResourceMapCache) -> None:
        result = await cache.get_int_id("file", "/a.txt", "zone1")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_int_id(self, cache: ResourceMapCache) -> None:
        await cache.set_int_id("file", "/a.txt", "zone1", 42)
        result = await cache.get_int_id("file", "/a.txt", "zone1")
        assert result == 42

    @pytest.mark.asyncio
    async def test_bulk_get(self, cache: ResourceMapCache) -> None:
        await cache.set_int_id("file", "/a.txt", "zone1", 1)
        await cache.set_int_id("file", "/b.txt", "zone1", 2)
        resources = [("file", "/a.txt", "zone1"), ("file", "/b.txt", "zone1"), ("file", "/c.txt", "zone1")]
        results = await cache.get_int_ids_bulk(resources)
        assert results[("file", "/a.txt", "zone1")] == 1
        assert results[("file", "/b.txt", "zone1")] == 2
        assert results[("file", "/c.txt", "zone1")] is None

    @pytest.mark.asyncio
    async def test_bulk_set(self, cache: ResourceMapCache) -> None:
        mappings = {
            ("file", "/a.txt", "zone1"): 10,
            ("file", "/b.txt", "zone1"): 20,
        }
        await cache.set_int_ids_bulk(mappings)
        assert await cache.get_int_id("file", "/a.txt", "zone1") == 10
        assert await cache.get_int_id("file", "/b.txt", "zone1") == 20

    @pytest.mark.asyncio
    async def test_empty_bulk_operations(self, cache: ResourceMapCache) -> None:
        assert await cache.get_int_ids_bulk([]) == {}
        await cache.set_int_ids_bulk({})  # Should not raise


# ---------------------------------------------------------------------------
# EmbeddingCache functional tests
# ---------------------------------------------------------------------------


class TestEmbeddingCache:
    """Exercise EmbeddingCache methods with InMemoryCacheStore."""

    @pytest.fixture
    def cache(self, store: InMemoryCacheStore) -> EmbeddingCache:
        return EmbeddingCache(store, ttl=86400)

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache: EmbeddingCache) -> None:
        result = await cache.get("hello world", "text-embedding-3-small")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get(self, cache: EmbeddingCache) -> None:
        embedding = [0.1, 0.2, 0.3]
        await cache.set("hello", "model-1", embedding)
        result = await cache.get("hello", "model-1")
        assert result == embedding

    @pytest.mark.asyncio
    async def test_batch_get(self, cache: EmbeddingCache) -> None:
        await cache.set("a", "model-1", [0.1])
        await cache.set("b", "model-1", [0.2])
        results = await cache.get_batch(["a", "b", "c"], "model-1")
        assert results["a"] == [0.1]
        assert results["b"] == [0.2]
        assert results["c"] is None

    @pytest.mark.asyncio
    async def test_batch_set(self, cache: EmbeddingCache) -> None:
        await cache.set_batch({"x": [1.0], "y": [2.0]}, "model-1")
        assert await cache.get("x", "model-1") == [1.0]
        assert await cache.get("y", "model-1") == [2.0]

    @pytest.mark.asyncio
    async def test_get_or_embed_batch(self, cache: EmbeddingCache) -> None:
        """Test the main entry point that deduplicates and calls embed_fn for uncached."""
        await cache.set("cached", "model-1", [0.5])

        async def mock_embed(texts: list[str]) -> list[list[float]]:
            return [[float(len(t))] for t in texts]

        results = await cache.get_or_embed_batch(
            ["cached", "new1", "new2"], "model-1", mock_embed
        )
        assert results[0] == [0.5]  # from cache
        assert results[1] == [4.0]  # len("new1") = 4
        assert results[2] == [4.0]  # len("new2") = 4

    @pytest.mark.asyncio
    async def test_get_or_embed_batch_deduplication(self, cache: EmbeddingCache) -> None:
        """Duplicate texts should only call embed_fn once."""
        call_count = 0

        async def mock_embed(texts: list[str]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            return [[1.0] for _ in texts]

        results = await cache.get_or_embed_batch(
            ["same", "same", "same"], "model-1", mock_embed
        )
        assert len(results) == 3
        assert all(r == [1.0] for r in results)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_invalidate(self, cache: EmbeddingCache) -> None:
        await cache.set("hello", "model-1", [0.1])
        assert await cache.invalidate("hello", "model-1") is True
        assert await cache.get("hello", "model-1") is None

    @pytest.mark.asyncio
    async def test_clear_by_model(self, cache: EmbeddingCache) -> None:
        await cache.set("a", "model-1", [0.1])
        await cache.set("b", "model-2", [0.2])
        count = await cache.clear("model-1")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_health_check(self, cache: EmbeddingCache) -> None:
        assert await cache.health_check() is True

    @pytest.mark.asyncio
    async def test_metrics(self, cache: EmbeddingCache) -> None:
        await cache.get("miss", "model-1")  # miss
        await cache.set("hit", "model-1", [0.1])
        await cache.get("hit", "model-1")  # hit

        metrics = cache.get_metrics()
        assert metrics["hits"] == 1
        assert metrics["misses"] == 1
        assert metrics["errors"] == 0
        assert 0.0 <= metrics["hit_rate"] <= 1.0

    @pytest.mark.asyncio
    async def test_empty_batch_operations(self, cache: EmbeddingCache) -> None:
        assert await cache.get_batch([], "model-1") == {}
        await cache.set_batch({}, "model-1")  # no-op

        async def mock_embed(texts: list[str]) -> list[list[float]]:
            return []

        assert await cache.get_or_embed_batch([], "model-1", mock_embed) == []
