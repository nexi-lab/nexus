"""Test domain cache classes comply with their protocols (Issue #1524).

Verifies each (Protocol, Implementation) pair:
- PermissionCacheProtocol <-> PermissionCache
- TigerCacheProtocol <-> TigerCache
- ResourceMapCacheProtocol <-> ResourceMapCache
- EmbeddingCacheProtocol <-> EmbeddingCache
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
def cache_store():
    """Create a fresh InMemoryCacheStore for each test."""
    return InMemoryCacheStore()

class TestPermissionCacheCompliance:
    """PermissionCache satisfies PermissionCacheProtocol."""

    def test_isinstance_check(self, cache_store):
        cache = PermissionCache(cache_store)
        assert isinstance(cache, PermissionCacheProtocol)

    @pytest.mark.asyncio
    async def test_set_and_get(self, cache_store):
        cache = PermissionCache(cache_store)
        await cache.set(
            subject_type="user",
            subject_id="alice",
            permission="read",
            object_type="file",
            object_id="/test.txt",
            result=True,
            zone_id="z1",
        )
        result = await cache.get(
            subject_type="user",
            subject_id="alice",
            permission="read",
            object_type="file",
            object_id="/test.txt",
            zone_id="z1",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache_store):
        cache = PermissionCache(cache_store)
        result = await cache.get(
            subject_type="user",
            subject_id="unknown",
            permission="read",
            object_type="file",
            object_id="/missing.txt",
            zone_id="z1",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_invalidate_subject(self, cache_store):
        cache = PermissionCache(cache_store)
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "z1")
        count = await cache.invalidate_subject("user", "alice", "z1")
        assert count >= 1

        result = await cache.get("user", "alice", "read", "file", "/a.txt", "z1")
        assert result is None

    @pytest.mark.asyncio
    async def test_clear(self, cache_store):
        cache = PermissionCache(cache_store)
        await cache.set("user", "alice", "read", "file", "/a.txt", True, "z1")
        count = await cache.clear(zone_id="z1")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_health_check(self, cache_store):
        cache = PermissionCache(cache_store)
        assert await cache.health_check() is True

class TestTigerCacheCompliance:
    """TigerCache satisfies TigerCacheProtocol."""

    def test_isinstance_check(self, cache_store):
        cache = TigerCache(cache_store)
        assert isinstance(cache, TigerCacheProtocol)

    @pytest.mark.asyncio
    async def test_set_and_get_bitmap(self, cache_store):
        cache = TigerCache(cache_store)
        bitmap = b"\x01\x02\x03"
        await cache.set_bitmap(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            zone_id="z1",
            bitmap_data=bitmap,
            revision=1,
        )
        result = await cache.get_bitmap(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            zone_id="z1",
        )
        assert result is not None
        assert result[0] == bitmap
        assert result[1] == 1

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache_store):
        cache = TigerCache(cache_store)
        result = await cache.get_bitmap("user", "unknown", "read", "file", "z1")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalidate(self, cache_store):
        cache = TigerCache(cache_store)
        await cache.set_bitmap("user", "alice", "read", "file", "z1", b"\x01", 1)
        count = await cache.invalidate(subject_type="user", subject_id="alice")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_health_check(self, cache_store):
        cache = TigerCache(cache_store)
        assert await cache.health_check() is True

class TestResourceMapCacheCompliance:
    """ResourceMapCache satisfies ResourceMapCacheProtocol."""

    def test_isinstance_check(self, cache_store):
        cache = ResourceMapCache(cache_store)
        assert isinstance(cache, ResourceMapCacheProtocol)

    @pytest.mark.asyncio
    async def test_set_and_get_int_id(self, cache_store):
        cache = ResourceMapCache(cache_store)
        await cache.set_int_id("file", "/test.txt", "z1", 42)
        result = await cache.get_int_id("file", "/test.txt", "z1")
        assert result == 42

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache_store):
        cache = ResourceMapCache(cache_store)
        result = await cache.get_int_id("file", "/missing.txt", "z1")
        assert result is None

class TestEmbeddingCacheCompliance:
    """EmbeddingCache satisfies EmbeddingCacheProtocol."""

    def test_isinstance_check(self, cache_store):
        cache = EmbeddingCache(cache_store)
        assert isinstance(cache, EmbeddingCacheProtocol)

    @pytest.mark.asyncio
    async def test_set_and_get(self, cache_store):
        cache = EmbeddingCache(cache_store)
        embedding = [0.1, 0.2, 0.3]
        await cache.set("model1", "hello world", embedding)
        result = await cache.get("model1", "hello world")
        assert result is not None
        assert len(result) == 3
        assert abs(result[0] - 0.1) < 0.001

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, cache_store):
        cache = EmbeddingCache(cache_store)
        result = await cache.get("model1", "unknown text")
        assert result is None

    @pytest.mark.asyncio
    async def test_health_check(self, cache_store):
        cache = EmbeddingCache(cache_store)
        assert await cache.health_check() is True
