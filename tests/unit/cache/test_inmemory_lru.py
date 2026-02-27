"""TDD tests for InMemoryCacheStore LRU eviction (Issue #1524, Decision #15).

Tests the max_size + LRU eviction enhancement to InMemoryCacheStore.
"""

import pytest

# ---------------------------------------------------------------------------
# LRU eviction tests
# ---------------------------------------------------------------------------


class TestInMemoryLRU:
    """Test LRU eviction in InMemoryCacheStore."""

    @pytest.mark.asyncio
    async def test_max_size_evicts_lru(self) -> None:
        """When max_size is hit, oldest entry should be evicted."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=3)
        await store.set("a", b"1")
        await store.set("b", b"2")
        await store.set("c", b"3")
        # Store is full (3 items). Adding "d" should evict "a" (LRU)
        await store.set("d", b"4")
        assert await store.get("a") is None  # Evicted
        assert await store.get("d") == b"4"  # Present

    @pytest.mark.asyncio
    async def test_get_refreshes_lru_order(self) -> None:
        """Accessing a key should refresh its LRU position."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=3)
        await store.set("a", b"1")
        await store.set("b", b"2")
        await store.set("c", b"3")
        # Access "a" — refreshes it
        await store.get("a")
        # Now "b" is the LRU. Adding "d" should evict "b"
        await store.set("d", b"4")
        assert await store.get("a") == b"1"  # Refreshed, still present
        assert await store.get("b") is None  # Evicted (was LRU)

    @pytest.mark.asyncio
    async def test_set_existing_key_refreshes(self) -> None:
        """Overwriting an existing key should refresh its LRU position."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=3)
        await store.set("a", b"1")
        await store.set("b", b"2")
        await store.set("c", b"3")
        # Overwrite "a" — refreshes it
        await store.set("a", b"1-new")
        # "b" is now LRU
        await store.set("d", b"4")
        assert await store.get("a") == b"1-new"  # Still present
        assert await store.get("b") is None  # Evicted

    @pytest.mark.asyncio
    async def test_max_size_zero_unlimited(self) -> None:
        """max_size=0 should mean unlimited (backward compat)."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=0)
        for i in range(1000):
            await store.set(f"key{i}", f"val{i}".encode())
        # All 1000 keys should be present
        assert await store.get("key0") == b"val0"
        assert await store.get("key999") == b"val999"

    @pytest.mark.asyncio
    async def test_default_max_size_unlimited(self) -> None:
        """Default constructor should have unlimited size (backward compat)."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore()
        for i in range(100):
            await store.set(f"key{i}", f"val{i}".encode())
        assert await store.get("key0") == b"val0"

    @pytest.mark.asyncio
    async def test_eviction_order_correct(self) -> None:
        """Eviction should follow strict LRU order."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=3)
        await store.set("a", b"1")
        await store.set("b", b"2")
        await store.set("c", b"3")
        # "a" is LRU. Evict "a"
        await store.set("d", b"4")
        assert await store.get("a") is None
        # Now "b" is LRU. Evict "b"
        await store.set("e", b"5")
        assert await store.get("b") is None
        # "c", "d", "e" remain
        assert await store.get("c") == b"3"
        assert await store.get("d") == b"4"
        assert await store.get("e") == b"5"

    @pytest.mark.asyncio
    async def test_stats_track_evictions(self) -> None:
        """InMemoryCacheStore should track eviction count in stats."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=2)
        await store.set("a", b"1")
        await store.set("b", b"2")
        await store.set("c", b"3")  # Evicts "a"
        await store.set("d", b"4")  # Evicts "b"
        stats = store.get_stats()
        assert stats["evictions"] == 2
        assert stats["max_size"] == 2
        assert stats["current_size"] == 2

    @pytest.mark.asyncio
    async def test_ttl_expiry_frees_slot(self) -> None:
        """Expired entries should not count toward max_size during eviction check."""
        from nexus.cache.inmemory import InMemoryCacheStore

        store = InMemoryCacheStore(max_size=2)
        # Set with very short TTL — will expire on next access
        await store.set("a", b"1", ttl=-1)  # Already expired
        await store.set("b", b"2")
        # "a" is expired — only "b" is real. Adding "c" should be fine.
        await store.set("c", b"3")
        assert await store.get("a") is None  # Expired
        assert await store.get("b") is not None or await store.get("c") is not None
