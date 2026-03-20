"""Unit tests for CASAddressingEngine metadata LRU cache (Issue #2940).

Tests cover:
- Meta cache hit/miss counters
- cache_stats property
- Cache population on _read_meta miss
- Cache update on _write_meta
- Cache eviction on delete_content (last ref)
- Cache bypass when meta_cache is None (cloud backends)
"""

import cachetools
import pytest

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from tests.unit.backends.test_cas_backend import InMemoryBlobTransport


@pytest.fixture
def transport() -> InMemoryBlobTransport:
    return InMemoryBlobTransport()


@pytest.fixture
def meta_cache() -> cachetools.LRUCache:
    return cachetools.LRUCache(maxsize=100)


@pytest.fixture
def backend(
    transport: InMemoryBlobTransport, meta_cache: cachetools.LRUCache
) -> CASAddressingEngine:
    return CASAddressingEngine(transport, backend_name="test-cas", meta_cache=meta_cache)


@pytest.fixture
def backend_no_cache(transport: InMemoryBlobTransport) -> CASAddressingEngine:
    """Backend without meta_cache (simulates cloud backends)."""
    return CASAddressingEngine(transport, backend_name="test-cas-no-cache")


class TestCacheStats:
    """Test cache_stats property."""

    def test_initial_stats_empty(self, backend: CASAddressingEngine):
        stats = backend.cache_stats
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0
        assert stats["maxsize"] == 100

    def test_stats_without_cache(self, backend_no_cache: CASAddressingEngine):
        stats = backend_no_cache.cache_stats
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0
        assert stats["maxsize"] == 0


class TestMetaCacheReadThrough:
    """Test _read_meta with cache read-through."""

    def test_first_read_is_miss(self, backend: CASAddressingEngine):
        content = b"test content"
        backend.write_content(content)

        # write_content calls _read_meta (miss) + _write_meta (populates cache)
        # The initial _read_meta in _meta_update_locked is a miss
        assert backend.cache_stats["misses"] >= 1

    def test_second_read_is_hit(self, backend: CASAddressingEngine):
        content = b"test content"
        result = backend.write_content(content)

        # Reset counters to isolate the read
        backend._meta_cache_hits = 0
        backend._meta_cache_misses = 0

        # Read meta again — should be a cache hit
        meta = backend._read_meta(result.content_id)
        assert meta["ref_count"] == 1
        assert backend.cache_stats["hits"] == 1
        assert backend.cache_stats["misses"] == 0

    def test_cache_populated_after_miss(
        self, backend: CASAddressingEngine, meta_cache: cachetools.LRUCache
    ):
        content = b"populate cache"
        result = backend.write_content(content)

        # After write, cache should contain the hash
        assert result.content_id in meta_cache
        assert meta_cache[result.content_id]["ref_count"] == 1

    def test_no_cache_no_error(self, backend_no_cache: CASAddressingEngine):
        """Backend without cache should work normally."""
        content = b"no cache content"
        result = backend_no_cache.write_content(content)
        data = backend_no_cache.read_content(result.content_id)
        assert data == content


class TestMetaCacheWriteThrough:
    """Test that _write_meta updates the cache."""

    def test_write_meta_updates_cache(
        self, backend: CASAddressingEngine, meta_cache: cachetools.LRUCache
    ):
        content = b"write-through test"
        result = backend.write_content(content)

        # Verify cache has the metadata
        cached_meta = meta_cache.get(result.content_id)
        assert cached_meta is not None
        assert cached_meta["ref_count"] == 1

        # Write again (ref_count bump)
        backend.write_content(content)
        cached_meta = meta_cache.get(result.content_id)
        assert cached_meta["ref_count"] == 2


class TestMetaCacheEviction:
    """Test cache eviction on delete_content."""

    def test_delete_last_ref_evicts_cache(
        self, backend: CASAddressingEngine, meta_cache: cachetools.LRUCache
    ):
        content = b"delete me"
        result = backend.write_content(content)
        h = result.content_id

        assert h in meta_cache

        backend.delete_content(h)

        # Cache entry should be evicted
        assert h not in meta_cache

    def test_delete_decrement_keeps_cache(
        self, backend: CASAddressingEngine, meta_cache: cachetools.LRUCache
    ):
        content = b"keep in cache"
        result = backend.write_content(content)
        backend.write_content(content)  # ref_count = 2
        h = result.content_id

        backend.delete_content(h)

        # ref_count decremented but not deleted — cache updated
        assert h in meta_cache
        assert meta_cache[h]["ref_count"] == 1


class TestMetaCacheLRUBehavior:
    """Test LRU eviction behavior."""

    def test_cache_maxsize_respected(self, transport: InMemoryBlobTransport):
        small_cache = cachetools.LRUCache(maxsize=3)
        backend = CASAddressingEngine(transport, backend_name="test", meta_cache=small_cache)

        hashes = []
        for i in range(5):
            result = backend.write_content(f"content-{i}".encode())
            hashes.append(result.content_id)

        # Only 3 entries should remain (LRU evicts oldest)
        assert len(small_cache) == 3
        assert backend.cache_stats["size"] == 3
        assert backend.cache_stats["maxsize"] == 3
