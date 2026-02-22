"""TDD tests for batch cache operations (Issue #1524, Decision #13).

Tests MGET/pipeline optimizations for DragonflyCacheStore.
Also tests batch fallback behavior on errors.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.cache.inmemory import InMemoryCacheStore

# ---------------------------------------------------------------------------
# Batch get operations
# ---------------------------------------------------------------------------


class TestBatchGetOperations:
    """Test get_many() batch behavior."""

    @pytest.mark.asyncio
    async def test_get_many_inmemory(self) -> None:
        """InMemory get_many should use sequential fallback by default."""
        store = InMemoryCacheStore()
        await store.set("k1", b"v1")
        await store.set("k2", b"v2")
        result = await store.get_many(["k1", "k2", "k3"])
        assert result == {"k1": b"v1", "k2": b"v2", "k3": None}

    @pytest.mark.asyncio
    async def test_get_many_empty_list(self) -> None:
        """get_many([]) should return empty dict."""
        store = InMemoryCacheStore()
        result = await store.get_many([])
        assert result == {}


# ---------------------------------------------------------------------------
# Batch set operations
# ---------------------------------------------------------------------------


class TestBatchSetOperations:
    """Test set_many() batch behavior."""

    @pytest.mark.asyncio
    async def test_set_many_inmemory(self) -> None:
        """InMemory set_many should store all keys."""
        store = InMemoryCacheStore()
        await store.set_many({"k1": b"v1", "k2": b"v2"}, ttl=300)
        assert await store.get("k1") == b"v1"
        assert await store.get("k2") == b"v2"

    @pytest.mark.asyncio
    async def test_set_many_empty_dict(self) -> None:
        """set_many({}) should be no-op."""
        store = InMemoryCacheStore()
        await store.set_many({})  # Should not raise


# ---------------------------------------------------------------------------
# Batch delete operations
# ---------------------------------------------------------------------------


class TestBatchDeleteOperations:
    """Test delete_by_pattern batch behavior."""

    @pytest.mark.asyncio
    async def test_delete_by_pattern_inmemory(self) -> None:
        """InMemory delete_by_pattern should use fnmatch."""
        store = InMemoryCacheStore()
        await store.set("perm:z1:user:alice:read:file:/a", b"1")
        await store.set("perm:z1:user:alice:write:file:/a", b"1")
        await store.set("perm:z1:user:bob:read:file:/a", b"1")
        # Delete all alice permissions
        count = await store.delete_by_pattern("perm:z1:user:alice:*")
        assert count == 2
        # Bob should still exist
        assert await store.get("perm:z1:user:bob:read:file:/a") == b"1"

    @pytest.mark.asyncio
    async def test_delete_by_pattern_no_matches(self) -> None:
        """delete_by_pattern with no matches returns 0."""
        store = InMemoryCacheStore()
        await store.set("key1", b"v1")
        count = await store.delete_by_pattern("nonexistent:*")
        assert count == 0


# ---------------------------------------------------------------------------
# Dragonfly batch ops (mocked)
# ---------------------------------------------------------------------------


class TestDragonflyBatchOps:
    """Test that DragonflyCacheStore uses MGET/pipeline for batch ops."""

    @pytest.mark.asyncio
    async def test_get_many_uses_mget(self) -> None:
        """DragonflyCacheStore.get_many() should use MGET."""
        from nexus.cache.dragonfly import DragonflyCacheStore

        mock_client = MagicMock()
        mock_redis = AsyncMock()
        mock_client.client = mock_redis
        mock_redis.mget = AsyncMock(return_value=[b"v1", None, b"v3"])

        store = DragonflyCacheStore(mock_client)
        result = await store.get_many(["k1", "k2", "k3"])

        mock_redis.mget.assert_awaited_once_with(["k1", "k2", "k3"])
        assert result == {"k1": b"v1", "k2": None, "k3": b"v3"}

    @pytest.mark.asyncio
    async def test_set_many_uses_pipeline(self) -> None:
        """DragonflyCacheStore.set_many() should use pipeline."""
        from nexus.cache.dragonfly import DragonflyCacheStore

        mock_client = MagicMock()
        mock_redis = AsyncMock()
        mock_pipe = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        mock_client.client = mock_redis

        store = DragonflyCacheStore(mock_client)
        await store.set_many({"k1": b"v1", "k2": b"v2"}, ttl=300)

        mock_redis.pipeline.assert_called_once()
        assert mock_pipe.setex.call_count == 2
        mock_pipe.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_by_pattern_uses_pipeline(self) -> None:
        """DragonflyCacheStore.delete_by_pattern() should pipeline deletes."""
        from nexus.cache.dragonfly import DragonflyCacheStore

        mock_client = MagicMock()
        mock_redis = AsyncMock()
        mock_client.client = mock_redis

        # Mock scan_iter to return keys
        async def fake_scan_iter(match=None, count=None):
            for key in [b"k1", b"k2", b"k3"]:
                yield key

        mock_redis.scan_iter = fake_scan_iter

        mock_pipe = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[1, 1, 1])

        store = DragonflyCacheStore(mock_client)
        count = await store.delete_by_pattern("prefix:*")

        assert count == 3
        mock_redis.pipeline.assert_called()

    @pytest.mark.asyncio
    async def test_get_many_fallback_on_error(self) -> None:
        """get_many should fall back to sequential gets on MGET error."""
        from nexus.cache.dragonfly import DragonflyCacheStore

        mock_client = MagicMock()
        mock_redis = AsyncMock()
        mock_client.client = mock_redis
        mock_redis.mget = AsyncMock(side_effect=Exception("MGET failed"))
        mock_redis.get = AsyncMock(return_value=b"v1")

        store = DragonflyCacheStore(mock_client)
        result = await store.get_many(["k1"])

        # Should have fallen back to individual gets
        assert result["k1"] == b"v1"
