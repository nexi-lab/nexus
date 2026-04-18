"""Tests for the 'inmem' cache backend option (Issue #3778)."""

import pytest

from nexus.cache.settings import CacheSettings


class TestInMemCacheBackend:
    def test_inmem_accepted(self) -> None:
        settings = CacheSettings(cache_backend="inmem")
        assert settings.cache_backend == "inmem"

    def test_inmem_does_not_require_dragonfly_url(self) -> None:
        settings = CacheSettings(cache_backend="inmem", dragonfly_url=None)
        settings.validate()  # should not raise

    def test_invalid_backend_still_rejected(self) -> None:
        with pytest.raises(ValueError):
            CacheSettings(cache_backend="bogus").validate()


class TestCacheFactoryInMem:
    @pytest.mark.asyncio
    async def test_inmem_backend_builds_inmemory_store(self) -> None:
        from nexus.cache.factory import CacheFactory
        from nexus.contracts.cache_store import InMemoryCacheStore

        settings = CacheSettings(cache_backend="inmem", dragonfly_url=None)
        factory = CacheFactory(settings)
        await factory.initialize()
        try:
            assert isinstance(factory._cache_store, InMemoryCacheStore)
            assert factory._has_cache_store is True
        finally:
            await factory.shutdown()

    @pytest.mark.asyncio
    async def test_inmem_backend_basic_get_set(self) -> None:
        from nexus.cache.factory import CacheFactory

        settings = CacheSettings(cache_backend="inmem")
        factory = CacheFactory(settings)
        await factory.initialize()
        try:
            store = factory._cache_store
            await store.set("k", b"v")
            assert await store.get("k") == b"v"
        finally:
            await factory.shutdown()
