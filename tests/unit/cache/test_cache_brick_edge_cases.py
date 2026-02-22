"""TDD tests for CacheBrick edge cases (Issue #1524).

Covers boundary conditions, error handling, and unusual inputs.
"""

import pytest

from nexus.cache.inmemory import InMemoryCacheStore
from nexus.cache.settings import CacheSettings

# ---------------------------------------------------------------------------
# Null store fallback on failure
# ---------------------------------------------------------------------------


class TestNullStoreFallback:
    """Test NullCacheStore fallback behaviors."""

    @pytest.mark.asyncio
    async def test_null_store_get_returns_none(self) -> None:
        """NullCacheStore.get() always returns None."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        result = await brick.cache_store.get("any_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_null_store_set_is_noop(self) -> None:
        """NullCacheStore.set() silently does nothing."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        await brick.cache_store.set("key", b"value", ttl=300)
        # No error, no storage
        result = await brick.cache_store.get("key")
        assert result is None

    @pytest.mark.asyncio
    async def test_pubsub_with_null_store(self) -> None:
        """PubSub with NullCacheStore should return 0 receivers."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        count = await brick.cache_store.publish("channel", b"msg")
        assert count == 0


# ---------------------------------------------------------------------------
# Key/value edge cases
# ---------------------------------------------------------------------------


class TestKeyValueEdgeCases:
    """Test edge cases in cache key/value handling."""

    @pytest.mark.asyncio
    async def test_empty_prefix_handling(self) -> None:
        """Domain caches should handle empty zone_id prefix."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick(cache_store=InMemoryCacheStore())
        # Permission cache with empty zone_id
        perm = brick.permission_cache
        await perm.set("user", "alice", "read", "file", "/test", True, "")
        result = await perm.get("user", "alice", "read", "file", "/test", "")
        assert result is True

    @pytest.mark.asyncio
    async def test_very_long_key_handling(self) -> None:
        """CacheStore should handle very long keys without error."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        long_key = "a" * 10000
        await store.set(long_key, b"value")
        result = await store.get(long_key)
        assert result == b"value"

    @pytest.mark.asyncio
    async def test_binary_value_handling(self) -> None:
        """CacheStore should handle binary values correctly."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        binary_data = bytes(range(256))
        await store.set("binary", binary_data)
        result = await store.get("binary")
        assert result == binary_data

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key(self) -> None:
        """Deleting a non-existent key should return False."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        result = await store.delete("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# TTL edge cases
# ---------------------------------------------------------------------------


class TestTTLEdgeCases:
    """Test TTL boundary conditions."""

    @pytest.mark.asyncio
    async def test_ttl_zero_means_no_expiry(self) -> None:
        """TTL=None should mean the key never expires."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        await store.set("forever", b"value", ttl=None)
        result = await store.get("forever")
        assert result == b"value"

    @pytest.mark.asyncio
    async def test_ttl_negative_ignored(self) -> None:
        """Negative TTL should still set (implementation may vary)."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        # Negative TTL means already expired — get should return None
        await store.set("negative_ttl", b"value", ttl=-1)
        result = await store.get("negative_ttl")
        assert result is None  # Expired immediately


# ---------------------------------------------------------------------------
# Batch operation edge cases
# ---------------------------------------------------------------------------


class TestBatchEdgeCases:
    """Test batch operation boundary conditions."""

    @pytest.mark.asyncio
    async def test_get_many_partial_miss(self) -> None:
        """get_many with mix of hits and misses."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        await store.set("key1", b"val1")
        # key2 does not exist
        results = await store.get_many(["key1", "key2"])
        assert results["key1"] == b"val1"
        assert results["key2"] is None

    @pytest.mark.asyncio
    async def test_set_many_empty_dict(self) -> None:
        """set_many with empty dict should be no-op."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        await store.set_many({})  # Should not raise

    @pytest.mark.asyncio
    async def test_pattern_delete_no_matches(self) -> None:
        """delete_by_pattern with no matches should return 0."""
        from nexus.cache.brick import CacheBrick

        store = InMemoryCacheStore()
        CacheBrick(cache_store=store)  # Verify brick accepts this store
        count = await store.delete_by_pattern("nonexistent:*")
        assert count == 0


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


class TestSettingsValidation:
    """Test CacheSettings validation."""

    def test_settings_default_values(self) -> None:
        """Default CacheSettings should have sensible defaults."""
        settings = CacheSettings(dragonfly_url=None)
        assert settings.permission_ttl == 300
        assert settings.tiger_ttl == 3600
        assert settings.embedding_ttl == 86400

    def test_settings_from_env_no_url(self) -> None:
        """CacheSettings.from_env() with no DRAGONFLY_URL should work."""
        import os

        # Ensure no URL is set
        env = os.environ.copy()
        os.environ.pop("NEXUS_DRAGONFLY_URL", None)
        try:
            settings = CacheSettings.from_env()
            assert settings.dragonfly_url is None
        finally:
            os.environ.update(env)


# ---------------------------------------------------------------------------
# Hash caching consistency (Decision #16)
