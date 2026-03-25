"""Unit tests for CASAddressingEngine metadata LRU cache (Issue #2940).

Tests cover:
- Meta cache hit/miss counters
- cache_stats property
- Cache population on _read_meta miss (from .meta sidecar on disk)
- Cache update on _write_meta
- Cache eviction on delete_content
- Cache bypass when meta_cache is None (cloud backends)

Note: Non-CDC write_content no longer creates .meta sidecars.
Meta cache is only populated via _read_meta (CDC path) or _write_meta directly.
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
    """Test _read_meta with cache read-through.

    Non-CDC write_content does not create .meta files.
    We test cache behavior by calling _write_meta + _read_meta directly.
    """

    def test_read_meta_miss_returns_default(self, backend: CASAddressingEngine):
        """_read_meta for non-existent .meta returns default and increments miss."""
        meta = backend._read_meta("nonexistent_hash")
        assert meta == {"size": 0}
        assert backend.cache_stats["misses"] == 1

    def test_read_meta_populates_cache_on_miss(
        self, backend: CASAddressingEngine, meta_cache: cachetools.LRUCache
    ):
        """_read_meta stores the result in cache even on miss (default dict)."""
        h = "some_hash"
        backend._read_meta(h)
        assert h in meta_cache
        assert meta_cache[h] == {"size": 0}

    def test_second_read_is_hit(self, backend: CASAddressingEngine):
        """After _write_meta populates cache, _read_meta should be a hit."""
        h = "test_hash"
        meta = {"size": 42, "is_chunked_manifest": True}
        backend._write_meta(h, meta)

        # Reset counters to isolate the read
        backend._meta_cache_hits = 0
        backend._meta_cache_misses = 0

        # Read meta — should be a cache hit
        result = backend._read_meta(h)
        assert result["size"] == 42
        assert backend.cache_stats["hits"] == 1
        assert backend.cache_stats["misses"] == 0

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
        h = "write_through_hash"
        meta = {"size": 100, "chunk_count": 3}
        backend._write_meta(h, meta)

        # Verify cache has the metadata
        cached_meta = meta_cache.get(h)
        assert cached_meta is not None
        assert cached_meta["size"] == 100
        assert cached_meta["chunk_count"] == 3


class TestMetaCacheEviction:
    """Test cache eviction on delete_content."""

    def test_delete_evicts_cache(
        self, backend: CASAddressingEngine, meta_cache: cachetools.LRUCache
    ):
        content = b"delete me"
        result = backend.write_content(content)
        h = result.content_id

        # Manually populate cache (since non-CDC write doesn't create .meta)
        meta_cache[h] = {"size": len(content)}
        assert h in meta_cache

        backend.delete_content(h)

        # Cache entry should be evicted
        assert h not in meta_cache


class TestMetaCacheLRUBehavior:
    """Test LRU eviction behavior."""

    def test_cache_maxsize_respected(self, transport: InMemoryBlobTransport):
        small_cache = cachetools.LRUCache(maxsize=3)
        backend = CASAddressingEngine(transport, backend_name="test", meta_cache=small_cache)

        # Populate cache via _write_meta (since write_content no longer creates .meta)
        hashes = []
        for i in range(5):
            h = f"hash_{i}"
            backend._write_meta(h, {"size": i})
            hashes.append(h)

        # Only 3 entries should remain (LRU evicts oldest)
        assert len(small_cache) == 3
        assert backend.cache_stats["size"] == 3
        assert backend.cache_stats["maxsize"] == 3
