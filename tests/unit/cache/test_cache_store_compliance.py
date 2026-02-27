"""Parametrized CacheStoreABC compliance suite (Issue #1524, #9A).

Tests that InMemoryCacheStore and NullCacheStore both comply with the
CacheStoreABC contract. Each implementation must handle all abstract
methods correctly and return the documented types.
"""

import pytest

from nexus.cache.inmemory import InMemoryCacheStore
from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore

# ---------------------------------------------------------------------------
# Fixture: parametrize over both implementations
# ---------------------------------------------------------------------------


@pytest.fixture(params=[InMemoryCacheStore, NullCacheStore], ids=["inmemory", "null"])
def store(request: pytest.FixtureRequest) -> CacheStoreABC:
    """Create a fresh CacheStoreABC instance for each implementation."""
    return request.param()


# ---------------------------------------------------------------------------
# Contract compliance tests (parametrized over both implementations)
# ---------------------------------------------------------------------------


class TestCacheStoreCompliance:
    """Both InMemoryCacheStore and NullCacheStore satisfy CacheStoreABC."""

    @pytest.mark.asyncio
    async def test_isinstance_of_abc(self, store: CacheStoreABC) -> None:
        """Store must be a CacheStoreABC instance."""
        assert isinstance(store, CacheStoreABC)

    # --- get / set / delete ---

    @pytest.mark.asyncio
    async def test_get_missing_key_returns_none(self, store: CacheStoreABC) -> None:
        """get() on a non-existent key returns None."""
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_does_not_raise(self, store: CacheStoreABC) -> None:
        """set() completes without error."""
        await store.set("key1", b"value1")

    @pytest.mark.asyncio
    async def test_delete_missing_key_returns_bool(self, store: CacheStoreABC) -> None:
        """delete() on a missing key returns a bool."""
        result = await store.delete("nonexistent")
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_delete_after_set_returns_bool(self, store: CacheStoreABC) -> None:
        """delete() after set() returns a bool."""
        await store.set("key1", b"value1")
        result = await store.delete("key1")
        assert isinstance(result, bool)

    # --- get_many / set_many ---

    @pytest.mark.asyncio
    async def test_get_many_returns_dict(self, store: CacheStoreABC) -> None:
        """get_many() returns a dict keyed by the requested keys."""
        result = await store.get_many(["a", "b", "c"])
        assert isinstance(result, dict)
        assert set(result.keys()) == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_set_many_does_not_raise(self, store: CacheStoreABC) -> None:
        """set_many() completes without error."""
        await store.set_many({"x": b"1", "y": b"2"}, ttl=60)

    # --- delete_by_pattern ---

    @pytest.mark.asyncio
    async def test_delete_by_pattern_returns_int(self, store: CacheStoreABC) -> None:
        """delete_by_pattern() returns an int (count of deleted keys)."""
        result = await store.delete_by_pattern("prefix:*")
        assert isinstance(result, int)
        assert result >= 0

    # --- publish ---

    @pytest.mark.asyncio
    async def test_publish_returns_int(self, store: CacheStoreABC) -> None:
        """publish() returns an int (number of receivers)."""
        result = await store.publish("channel", b"message")
        assert isinstance(result, int)
        assert result >= 0

    # --- health_check ---

    @pytest.mark.asyncio
    async def test_health_check_returns_bool(self, store: CacheStoreABC) -> None:
        """health_check() returns a bool."""
        result = await store.health_check()
        assert isinstance(result, bool)

    # --- close ---

    @pytest.mark.asyncio
    async def test_close_is_safe(self, store: CacheStoreABC) -> None:
        """close() completes without error and can be called multiple times."""
        await store.close()
        await store.close()  # Second call should also be safe


# ---------------------------------------------------------------------------
# InMemoryCacheStore-specific correctness tests
# ---------------------------------------------------------------------------


class TestInMemoryCacheStoreCorrectness:
    """InMemoryCacheStore should actually store and retrieve data."""

    @pytest.mark.asyncio
    async def test_set_then_get_roundtrip(self) -> None:
        """Data written with set() is retrievable via get()."""
        store = InMemoryCacheStore()
        await store.set("key1", b"hello")
        assert await store.get("key1") == b"hello"

    @pytest.mark.asyncio
    async def test_set_overwrites_existing(self) -> None:
        """Setting the same key twice overwrites the value."""
        store = InMemoryCacheStore()
        await store.set("key1", b"first")
        await store.set("key1", b"second")
        assert await store.get("key1") == b"second"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self) -> None:
        """delete() removes the key so get() returns None."""
        store = InMemoryCacheStore()
        await store.set("key1", b"value")
        deleted = await store.delete("key1")
        assert deleted is True
        assert await store.get("key1") is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self) -> None:
        """delete() on a missing key returns False."""
        store = InMemoryCacheStore()
        assert await store.delete("missing") is False

    @pytest.mark.asyncio
    async def test_get_many_returns_stored_values(self) -> None:
        """get_many() returns values for keys that were set."""
        store = InMemoryCacheStore()
        await store.set("a", b"1")
        await store.set("b", b"2")
        result = await store.get_many(["a", "b", "c"])
        assert result["a"] == b"1"
        assert result["b"] == b"2"
        assert result["c"] is None

    @pytest.mark.asyncio
    async def test_set_many_stores_all_keys(self) -> None:
        """set_many() stores all keys so they are retrievable."""
        store = InMemoryCacheStore()
        await store.set_many({"x": b"10", "y": b"20"})
        assert await store.get("x") == b"10"
        assert await store.get("y") == b"20"

    @pytest.mark.asyncio
    async def test_delete_by_pattern_removes_matching(self) -> None:
        """delete_by_pattern() removes matching keys and returns count."""
        store = InMemoryCacheStore()
        await store.set("perm:zone1:alice", b"1")
        await store.set("perm:zone1:bob", b"2")
        await store.set("perm:zone2:alice", b"3")
        deleted = await store.delete_by_pattern("perm:zone1:*")
        assert deleted == 2
        assert await store.get("perm:zone1:alice") is None
        assert await store.get("perm:zone2:alice") == b"3"

    @pytest.mark.asyncio
    async def test_exists_reflects_state(self) -> None:
        """exists() returns True after set and False after delete."""
        store = InMemoryCacheStore()
        assert await store.exists("key1") is False
        await store.set("key1", b"val")
        assert await store.exists("key1") is True
        await store.delete("key1")
        assert await store.exists("key1") is False

    @pytest.mark.asyncio
    async def test_health_check_true_before_close(self) -> None:
        """health_check() returns True on a fresh store."""
        store = InMemoryCacheStore()
        assert await store.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_after_close(self) -> None:
        """health_check() returns False after close()."""
        store = InMemoryCacheStore()
        await store.close()
        assert await store.health_check() is False

    @pytest.mark.asyncio
    async def test_publish_returns_zero_with_no_subscribers(self) -> None:
        """publish() returns 0 when there are no subscribers."""
        store = InMemoryCacheStore()
        result = await store.publish("chan", b"msg")
        assert result == 0


# ---------------------------------------------------------------------------
# NullCacheStore-specific behaviour tests
# ---------------------------------------------------------------------------


class TestNullCacheStoreNoop:
    """NullCacheStore is a no-op: returns None/0/False for everything."""

    @pytest.mark.asyncio
    async def test_get_always_none(self) -> None:
        """get() always returns None regardless of prior set()."""
        store = NullCacheStore()
        await store.set("key1", b"value")
        assert await store.get("key1") is None

    @pytest.mark.asyncio
    async def test_delete_always_false(self) -> None:
        """delete() always returns False."""
        store = NullCacheStore()
        assert await store.delete("anything") is False

    @pytest.mark.asyncio
    async def test_exists_always_false(self) -> None:
        """exists() always returns False."""
        store = NullCacheStore()
        await store.set("key1", b"val")
        assert await store.exists("key1") is False

    @pytest.mark.asyncio
    async def test_delete_by_pattern_returns_zero(self) -> None:
        """delete_by_pattern() always returns 0."""
        store = NullCacheStore()
        assert await store.delete_by_pattern("*") == 0

    @pytest.mark.asyncio
    async def test_publish_returns_zero(self) -> None:
        """publish() always returns 0 receivers."""
        store = NullCacheStore()
        assert await store.publish("chan", b"msg") == 0

    @pytest.mark.asyncio
    async def test_health_check_returns_true(self) -> None:
        """health_check() returns True (NullCacheStore is always healthy)."""
        store = NullCacheStore()
        assert await store.health_check() is True

    @pytest.mark.asyncio
    async def test_get_many_all_none(self) -> None:
        """get_many() returns None for every key."""
        store = NullCacheStore()
        result = await store.get_many(["a", "b"])
        assert result == {"a": None, "b": None}

    @pytest.mark.asyncio
    async def test_subscribe_yields_empty_iterator(self) -> None:
        """subscribe() yields an async iterator that produces no messages."""
        store = NullCacheStore()
        messages: list[bytes] = []
        async with store.subscribe("channel") as msg_iter:
            async for msg in msg_iter:
                messages.append(msg)
        assert messages == []
