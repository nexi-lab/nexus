"""Unit tests for ReadSetAwareCache (Issue #1169).

TDD: These tests were written BEFORE the implementation.

Tests for the read-set-aware cache wrapper that provides precise
cache invalidation by tracking which resources each cache entry depends on.
"""

import threading

from nexus.core.read_set import AccessType, ReadSet, ReadSetRegistry
from nexus.storage.cache import _CACHE_MISS, MetadataCache
from nexus.storage.read_set_cache import ReadSetAwareCache


def _is_cached(cache: MetadataCache, path: str) -> bool:
    """Check if a path has a cache entry (not evicted/invalidated)."""
    result = cache.get_path(path)
    return result is not _CACHE_MISS


class TestReadSetAwareCachePutAndGet:
    """Tests for cache put/get operations with read set tracking."""

    def setup_method(self):
        self.registry = ReadSetRegistry()
        self.base_cache = MetadataCache(
            path_cache_size=64,
            list_cache_size=32,
            kv_cache_size=32,
            exists_cache_size=64,
            ttl_seconds=300,
        )
        self.cache = ReadSetAwareCache(
            base_cache=self.base_cache,
            registry=self.registry,
        )

    def test_put_without_read_set(self):
        """Cache works normally without read sets — backward compatible."""
        from nexus.core._metadata_generated import FileMetadata

        meta = FileMetadata(path="/test.txt", backend_name="local", physical_path="abc123", size=100, etag="abc123")
        self.cache.put_path("/test.txt", meta)

        result = self.base_cache.get_path("/test.txt")
        assert result is not None
        assert result.path == "/test.txt"

    def test_put_with_read_set(self):
        """Cache stores read set mapping when provided."""
        from nexus.core._metadata_generated import FileMetadata

        meta = FileMetadata(path="/test.txt", backend_name="local", physical_path="abc123", size=100, etag="abc123")
        rs = ReadSet(query_id="q1", zone_id="z1")
        rs.record_read("file", "/test.txt", revision=5)

        self.cache.put_path("/test.txt", meta, read_set=rs)

        # Value should be in cache
        result = self.base_cache.get_path("/test.txt")
        assert result is not None

        # Read set should be registered
        assert self.registry.get_read_set("q1") is not None

        # Internal mapping should exist
        assert "/test.txt" in self.cache._cache_key_to_query

    def test_stale_revision_at_insert_rejected(self):
        """Zookie pattern: skip caching if revision already advanced."""
        from nexus.core._metadata_generated import FileMetadata

        meta = FileMetadata(path="/test.txt", backend_name="local", physical_path="abc123", size=100, etag="abc123")
        rs = ReadSet(query_id="q1", zone_id="z1")
        rs.record_read("file", "/test.txt", revision=5)

        # Zone revision is 10, read was at revision 5 — stale!
        self.cache.put_path("/test.txt", meta, read_set=rs, zone_revision=10)

        # Should NOT be in cache (stale at insert time)
        # get_path returns _CACHE_MISS sentinel (not None) when entry doesn't exist
        from nexus.storage.cache import _CACHE_MISS

        result = self.base_cache.get_path("/test.txt")
        assert result is _CACHE_MISS

        stats = self.cache.get_stats()
        assert stats["stale_insert_rejections"] == 1


class TestReadSetAwareCacheInvalidation:
    """Tests for precise invalidation via read sets."""

    def setup_method(self):
        self.registry = ReadSetRegistry()
        self.base_cache = MetadataCache(
            path_cache_size=64,
            list_cache_size=32,
            kv_cache_size=32,
            exists_cache_size=64,
            ttl_seconds=300,
        )
        self.cache = ReadSetAwareCache(
            base_cache=self.base_cache,
            registry=self.registry,
        )

    def _put_file(self, path: str, query_id: str, revision: int = 5):
        """Helper to cache a file with a read set."""
        from nexus.core._metadata_generated import FileMetadata

        meta = FileMetadata(path=path, backend_name="local", physical_path=f"hash_{path}", size=100, etag=f"hash_{path}")
        rs = ReadSet(query_id=query_id, zone_id="z1")
        rs.record_read("file", path, revision=revision)
        self.cache.put_path(path, meta, read_set=rs)

    def test_invalidate_for_write_precise(self):
        """Only invalidates entries whose read sets overlap with the write."""
        self._put_file("/inbox/a.txt", "q1")
        self._put_file("/inbox/b.txt", "q2")
        self._put_file("/docs/readme.md", "q3")

        # Write to /inbox/a.txt should only invalidate q1
        self.cache.invalidate_for_write("/inbox/a.txt", revision=10, zone_id="z1")

        # a.txt should be invalidated
        assert not _is_cached(self.base_cache, "/inbox/a.txt")
        # b.txt and readme.md should still be cached
        assert _is_cached(self.base_cache, "/inbox/b.txt")
        assert _is_cached(self.base_cache, "/docs/readme.md")

        stats = self.cache.get_stats()
        assert stats["precise_invalidations"] >= 1

    def test_invalidate_for_write_no_overlap(self):
        """Write to unrelated path doesn't invalidate anything."""
        self._put_file("/inbox/a.txt", "q1")

        self.cache.invalidate_for_write("/docs/other.txt", revision=10, zone_id="z1")

        # Should still be cached
        assert _is_cached(self.base_cache, "/inbox/a.txt")

        stats = self.cache.get_stats()
        assert stats["skipped_invalidations"] >= 1

    def test_invalidate_for_write_directory_containment(self):
        """Directory read sets catch writes to files within the directory."""
        from nexus.core._metadata_generated import FileMetadata

        # Cache a directory listing result
        meta = FileMetadata(path="/inbox/a.txt", backend_name="local", physical_path="hash_a", size=100, etag="hash_a")
        rs = ReadSet(query_id="q_dir", zone_id="z1")
        rs.record_read("directory", "/inbox/", revision=5, access_type=AccessType.LIST)
        rs.record_read("file", "/inbox/a.txt", revision=5)
        self.cache.put_path("/inbox/a.txt", meta, read_set=rs)

        # Write a NEW file in /inbox/ — directory listing is now stale
        self.cache.invalidate_for_write("/inbox/new_file.txt", revision=10, zone_id="z1")

        # The cached entry whose read set included /inbox/ listing should be invalidated
        assert not _is_cached(self.base_cache, "/inbox/a.txt")

    def test_fallback_to_path_invalidation(self):
        """Entries without read sets use path-based invalidation."""
        from nexus.core._metadata_generated import FileMetadata

        # Cache without read set (legacy behavior)
        meta = FileMetadata(path="/legacy.txt", backend_name="local", physical_path="hash_legacy", size=100, etag="hash_legacy")
        self.cache.put_path("/legacy.txt", meta)  # No read_set

        # Invalidation should fall back to path-based
        self.cache.invalidate_for_write("/legacy.txt", revision=10)

        assert not _is_cached(self.base_cache, "/legacy.txt")

        stats = self.cache.get_stats()
        assert stats["fallback_invalidations"] >= 1


class TestReadSetAwareCacheEviction:
    """Tests for cache eviction → read set cleanup."""

    def test_eviction_cleans_up_read_set(self):
        """LRU eviction removes read set from registry."""
        registry = ReadSetRegistry()
        base_cache = MetadataCache(
            path_cache_size=3,  # Very small — forces eviction
            list_cache_size=4,
            kv_cache_size=4,
            exists_cache_size=4,
            ttl_seconds=300,
        )
        cache = ReadSetAwareCache(
            base_cache=base_cache,
            registry=registry,
        )

        from nexus.core._metadata_generated import FileMetadata

        # Fill cache to capacity
        for i in range(3):
            path = f"/file_{i}.txt"
            meta = FileMetadata(path=path, backend_name="local", physical_path=f"hash_{i}", size=100, etag=f"hash_{i}")
            rs = ReadSet(query_id=f"q_{i}", zone_id="z1")
            rs.record_read("file", path, revision=5)
            cache.put_path(path, meta, read_set=rs)

        # All 3 should be in registry
        assert len(registry) == 3

        # Add 4th entry — should evict LRU (q_0)
        meta = FileMetadata(path="/file_3.txt", backend_name="local", physical_path="hash_3", size=100, etag="hash_3")
        rs = ReadSet(query_id="q_3", zone_id="z1")
        rs.record_read("file", "/file_3.txt", revision=5)
        cache.put_path("/file_3.txt", meta, read_set=rs)

        # q_0 should be cleaned up from internal mapping
        assert "/file_0.txt" not in cache._cache_key_to_query
        # Registry should have been cleaned
        assert "q_0" not in cache._query_to_cache_keys

    def test_read_sets_bounded_by_cache_size(self):
        """Internal read set mappings never exceed cache entry count."""
        registry = ReadSetRegistry()
        base_cache = MetadataCache(
            path_cache_size=5,
            list_cache_size=4,
            kv_cache_size=4,
            exists_cache_size=4,
            ttl_seconds=300,
        )
        cache = ReadSetAwareCache(
            base_cache=base_cache,
            registry=registry,
        )

        from nexus.core._metadata_generated import FileMetadata

        # Insert many more than capacity
        for i in range(20):
            path = f"/file_{i}.txt"
            meta = FileMetadata(path=path, backend_name="local", physical_path=f"hash_{i}", size=100, etag=f"hash_{i}")
            rs = ReadSet(query_id=f"q_{i}", zone_id="z1")
            rs.record_read("file", path, revision=5)
            cache.put_path(path, meta, read_set=rs)

        # Internal mappings should be bounded
        assert len(cache._cache_key_to_query) <= 5
        assert len(cache._query_to_cache_keys) <= 5


class TestReadSetAwareCacheStats:
    """Tests for invalidation precision metrics."""

    def test_get_stats_includes_precision_metrics(self):
        registry = ReadSetRegistry()
        base_cache = MetadataCache(ttl_seconds=300)
        cache = ReadSetAwareCache(base_cache=base_cache, registry=registry)

        stats = cache.get_stats()
        assert "precise_invalidations" in stats
        assert "skipped_invalidations" in stats
        assert "fallback_invalidations" in stats
        assert "stale_insert_rejections" in stats
        assert "precision_ratio" in stats
        assert "read_set_count" in stats


class TestZoneRevisionCounter:
    """Tests for per-zone monotonic revision counter (Issue #1169)."""

    def test_increment_monotonic(self):
        """Revision always increases."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        mixin = NexusFSCoreMixin()
        mixin._init_zone_revision()

        values = [mixin._increment_zone_revision() for _ in range(100)]
        assert values == list(range(1, 101))

    def test_get_returns_current(self):
        """get_zone_revision returns the latest value."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        mixin = NexusFSCoreMixin()
        mixin._init_zone_revision()

        assert mixin._get_zone_revision() == 0
        mixin._increment_zone_revision()
        assert mixin._get_zone_revision() == 1

    def test_concurrent_increments_no_lost_updates(self):
        """50 threads * 100 increments = 5000 total, no lost updates."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        mixin = NexusFSCoreMixin()
        mixin._init_zone_revision()

        n_threads = 50
        n_per_thread = 100
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()  # Ensure all threads start simultaneously
            for _ in range(n_per_thread):
                mixin._increment_zone_revision()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert mixin._get_zone_revision() == n_threads * n_per_thread

    def test_concurrent_no_duplicate_values(self):
        """Each increment returns a unique value (no duplicates)."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        mixin = NexusFSCoreMixin()
        mixin._init_zone_revision()

        n_threads = 20
        n_per_thread = 50
        results: list[list[int]] = [[] for _ in range(n_threads)]
        barrier = threading.Barrier(n_threads)

        def worker(idx):
            barrier.wait()
            for _ in range(n_per_thread):
                results[idx].append(mixin._increment_zone_revision())

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Flatten and check uniqueness
        all_values = [v for thread_results in results for v in thread_results]
        assert len(all_values) == len(set(all_values)), "Duplicate revision values detected!"
        assert len(all_values) == n_threads * n_per_thread
