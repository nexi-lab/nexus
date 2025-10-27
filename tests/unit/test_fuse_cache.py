"""Unit tests for FUSE cache manager.

These tests verify the caching functionality for FUSE operations including
attribute caching, content caching, parsed content caching, and cache invalidation.
"""

from __future__ import annotations

import threading

import pytest

from nexus.fuse.cache import FUSECacheManager


@pytest.fixture
def cache_mgr() -> FUSECacheManager:
    """Create a cache manager with metrics enabled and short TTL."""
    return FUSECacheManager(
        attr_cache_size=10,
        attr_cache_ttl=1,  # 1 second TTL for testing
        content_cache_size=5,
        parsed_cache_size=5,
        enable_metrics=True,
    )


@pytest.fixture
def cache_mgr_no_metrics() -> FUSECacheManager:
    """Create a cache manager without metrics."""
    return FUSECacheManager(
        attr_cache_size=10,
        attr_cache_ttl=60,
        content_cache_size=5,
        parsed_cache_size=5,
        enable_metrics=False,
    )


class TestAttributeCache:
    """Test attribute caching functionality."""

    def test_cache_and_get_attr(self, cache_mgr: FUSECacheManager) -> None:
        """Test caching and retrieving file attributes."""
        attrs = {"st_mode": 0o100644, "st_size": 1024, "st_mtime": 123456}
        cache_mgr.cache_attr("/file.txt", attrs)

        cached = cache_mgr.get_attr("/file.txt")
        assert cached == attrs

    def test_get_attr_miss(self, cache_mgr: FUSECacheManager) -> None:
        """Test getting uncached attributes returns None."""
        result = cache_mgr.get_attr("/nonexistent")
        assert result is None

    def test_attr_cache_ttl_expiry(self, cache_mgr: FUSECacheManager) -> None:
        """Test that attribute cache entries expire after TTL.

        Note: This test uses a real sleep because cachetools.TTLCache uses
        time.monotonic() which is not affected by freezegun.
        """
        import time

        attrs = {"st_mode": 0o100644, "st_size": 1024}
        cache_mgr.cache_attr("/file.txt", attrs)

        # Should be cached immediately
        assert cache_mgr.get_attr("/file.txt") == attrs

        # Wait for TTL to expire (1 second + small buffer)
        time.sleep(1.1)

        # Should be expired now
        assert cache_mgr.get_attr("/file.txt") is None

    def test_attr_cache_overwrite(self, cache_mgr: FUSECacheManager) -> None:
        """Test that caching same path overwrites previous value."""
        attrs1 = {"st_mode": 0o100644, "st_size": 1024}
        attrs2 = {"st_mode": 0o100644, "st_size": 2048}

        cache_mgr.cache_attr("/file.txt", attrs1)
        cache_mgr.cache_attr("/file.txt", attrs2)

        assert cache_mgr.get_attr("/file.txt") == attrs2

    def test_attr_metrics_hits(self, cache_mgr: FUSECacheManager) -> None:
        """Test that attribute cache hits are tracked."""
        attrs = {"st_mode": 0o100644}
        cache_mgr.cache_attr("/file.txt", attrs)

        cache_mgr.get_attr("/file.txt")
        cache_mgr.get_attr("/file.txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["attr_hits"] == 2
        assert metrics["attr_misses"] == 0

    def test_attr_metrics_misses(self, cache_mgr: FUSECacheManager) -> None:
        """Test that attribute cache misses are tracked."""
        cache_mgr.get_attr("/file1.txt")
        cache_mgr.get_attr("/file2.txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["attr_hits"] == 0
        assert metrics["attr_misses"] == 2

    def test_attr_metrics_hit_rate(self, cache_mgr: FUSECacheManager) -> None:
        """Test attribute cache hit rate calculation."""
        attrs = {"st_mode": 0o100644}
        cache_mgr.cache_attr("/file.txt", attrs)

        cache_mgr.get_attr("/file.txt")  # hit
        cache_mgr.get_attr("/other.txt")  # miss

        metrics = cache_mgr.get_metrics()
        assert metrics["attr_hit_rate"] == 0.5


class TestContentCache:
    """Test content caching functionality."""

    def test_cache_and_get_content(self, cache_mgr: FUSECacheManager) -> None:
        """Test caching and retrieving file content."""
        content = b"Hello, World!"
        cache_mgr.cache_content("/file.txt", content)

        cached = cache_mgr.get_content("/file.txt")
        assert cached == content

    def test_get_content_miss(self, cache_mgr: FUSECacheManager) -> None:
        """Test getting uncached content returns None."""
        result = cache_mgr.get_content("/nonexistent")
        assert result is None

    def test_content_cache_lru_eviction(self, cache_mgr: FUSECacheManager) -> None:
        """Test that content cache evicts LRU entries when full."""
        # Cache size is 5, fill it up
        for i in range(5):
            cache_mgr.cache_content(f"/file{i}.txt", f"content{i}".encode())

        # All should be cached
        assert cache_mgr.get_content("/file0.txt") == b"content0"

        # Add one more (should evict file1 as it's LRU after we accessed file0)
        cache_mgr.cache_content("/file5.txt", b"content5")

        # file1 should be evicted (we just accessed file0)
        assert cache_mgr.get_content("/file1.txt") is None
        assert cache_mgr.get_content("/file5.txt") == b"content5"

    def test_content_cache_overwrite(self, cache_mgr: FUSECacheManager) -> None:
        """Test that caching same path overwrites previous content."""
        cache_mgr.cache_content("/file.txt", b"old")
        cache_mgr.cache_content("/file.txt", b"new")

        assert cache_mgr.get_content("/file.txt") == b"new"

    def test_content_metrics_hits(self, cache_mgr: FUSECacheManager) -> None:
        """Test that content cache hits are tracked."""
        cache_mgr.cache_content("/file.txt", b"content")

        cache_mgr.get_content("/file.txt")
        cache_mgr.get_content("/file.txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["content_hits"] == 2
        assert metrics["content_misses"] == 0

    def test_content_metrics_misses(self, cache_mgr: FUSECacheManager) -> None:
        """Test that content cache misses are tracked."""
        cache_mgr.get_content("/file1.txt")
        cache_mgr.get_content("/file2.txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["content_hits"] == 0
        assert metrics["content_misses"] == 2


class TestParsedCache:
    """Test parsed content caching functionality."""

    def test_cache_and_get_parsed(self, cache_mgr: FUSECacheManager) -> None:
        """Test caching and retrieving parsed content."""
        content = b"Parsed content"
        cache_mgr.cache_parsed("/file.pdf", "txt", content)

        cached = cache_mgr.get_parsed("/file.pdf", "txt")
        assert cached == content

    def test_get_parsed_miss(self, cache_mgr: FUSECacheManager) -> None:
        """Test getting uncached parsed content returns None."""
        result = cache_mgr.get_parsed("/nonexistent", "txt")
        assert result is None

    def test_parsed_cache_different_view_types(self, cache_mgr: FUSECacheManager) -> None:
        """Test that different view types are cached separately."""
        cache_mgr.cache_parsed("/file.pdf", "txt", b"text version")
        cache_mgr.cache_parsed("/file.pdf", "md", b"markdown version")

        assert cache_mgr.get_parsed("/file.pdf", "txt") == b"text version"
        assert cache_mgr.get_parsed("/file.pdf", "md") == b"markdown version"

    def test_parsed_cache_lru_eviction(self, cache_mgr: FUSECacheManager) -> None:
        """Test that parsed cache evicts LRU entries when full."""
        # Cache size is 5, fill it up
        for i in range(5):
            cache_mgr.cache_parsed(f"/file{i}.pdf", "txt", f"content{i}".encode())

        # All should be cached
        assert cache_mgr.get_parsed("/file0.pdf", "txt") == b"content0"

        # Add one more (should evict file1 as it's LRU)
        cache_mgr.cache_parsed("/file5.pdf", "txt", b"content5")

        # file1 should be evicted
        assert cache_mgr.get_parsed("/file1.pdf", "txt") is None
        assert cache_mgr.get_parsed("/file5.pdf", "txt") == b"content5"

    def test_parsed_metrics_hits(self, cache_mgr: FUSECacheManager) -> None:
        """Test that parsed cache hits are tracked."""
        cache_mgr.cache_parsed("/file.pdf", "txt", b"content")

        cache_mgr.get_parsed("/file.pdf", "txt")
        cache_mgr.get_parsed("/file.pdf", "txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["parsed_hits"] == 2
        assert metrics["parsed_misses"] == 0

    def test_parsed_metrics_misses(self, cache_mgr: FUSECacheManager) -> None:
        """Test that parsed cache misses are tracked."""
        cache_mgr.get_parsed("/file1.pdf", "txt")
        cache_mgr.get_parsed("/file2.pdf", "txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["parsed_hits"] == 0
        assert metrics["parsed_misses"] == 2


class TestCacheInvalidation:
    """Test cache invalidation functionality."""

    def test_invalidate_path_clears_all_caches(self, cache_mgr: FUSECacheManager) -> None:
        """Test that invalidating a path clears all related caches."""
        # Cache data in all three caches
        cache_mgr.cache_attr("/file.pdf", {"st_mode": 0o100644})
        cache_mgr.cache_content("/file.pdf", b"content")
        cache_mgr.cache_parsed("/file.pdf", "txt", b"parsed")
        cache_mgr.cache_parsed("/file.pdf", "md", b"markdown")

        # Invalidate
        cache_mgr.invalidate_path("/file.pdf")

        # All should be gone
        assert cache_mgr.get_attr("/file.pdf") is None
        assert cache_mgr.get_content("/file.pdf") is None
        assert cache_mgr.get_parsed("/file.pdf", "txt") is None
        assert cache_mgr.get_parsed("/file.pdf", "md") is None

    def test_invalidate_path_only_affects_target(self, cache_mgr: FUSECacheManager) -> None:
        """Test that invalidation only affects the target path."""
        cache_mgr.cache_attr("/file1.txt", {"st_mode": 0o100644})
        cache_mgr.cache_attr("/file2.txt", {"st_mode": 0o100644})

        cache_mgr.invalidate_path("/file1.txt")

        assert cache_mgr.get_attr("/file1.txt") is None
        assert cache_mgr.get_attr("/file2.txt") is not None

    def test_invalidate_all_clears_everything(self, cache_mgr: FUSECacheManager) -> None:
        """Test that invalidate_all clears all caches."""
        # Fill all caches
        cache_mgr.cache_attr("/file1.txt", {"st_mode": 0o100644})
        cache_mgr.cache_attr("/file2.txt", {"st_mode": 0o100644})
        cache_mgr.cache_content("/file1.txt", b"content1")
        cache_mgr.cache_content("/file2.txt", b"content2")
        cache_mgr.cache_parsed("/file1.pdf", "txt", b"parsed1")
        cache_mgr.cache_parsed("/file2.pdf", "txt", b"parsed2")

        cache_mgr.invalidate_all()

        # Everything should be cleared
        assert cache_mgr.get_attr("/file1.txt") is None
        assert cache_mgr.get_attr("/file2.txt") is None
        assert cache_mgr.get_content("/file1.txt") is None
        assert cache_mgr.get_content("/file2.txt") is None
        assert cache_mgr.get_parsed("/file1.pdf", "txt") is None
        assert cache_mgr.get_parsed("/file2.pdf", "txt") is None

    def test_invalidate_increments_metrics(self, cache_mgr: FUSECacheManager) -> None:
        """Test that invalidation increments metrics counter."""
        cache_mgr.cache_attr("/file.txt", {"st_mode": 0o100644})

        cache_mgr.invalidate_path("/file.txt")
        cache_mgr.invalidate_path("/file.txt")

        metrics = cache_mgr.get_metrics()
        assert metrics["invalidations"] == 2


class TestMetrics:
    """Test metrics functionality."""

    def test_get_metrics_when_disabled(self, cache_mgr_no_metrics: FUSECacheManager) -> None:
        """Test that get_metrics returns empty dict when disabled."""
        metrics = cache_mgr_no_metrics.get_metrics()
        assert metrics == {}

    def test_get_metrics_includes_cache_sizes(self, cache_mgr: FUSECacheManager) -> None:
        """Test that metrics include current cache sizes."""
        cache_mgr.cache_attr("/file1.txt", {"st_mode": 0o100644})
        cache_mgr.cache_attr("/file2.txt", {"st_mode": 0o100644})
        cache_mgr.cache_content("/file1.txt", b"content")
        cache_mgr.cache_parsed("/file1.pdf", "txt", b"parsed")

        metrics = cache_mgr.get_metrics()
        assert metrics["cache_sizes"]["attr"] == 2
        assert metrics["cache_sizes"]["content"] == 1
        assert metrics["cache_sizes"]["parsed"] == 1

    def test_reset_metrics(self, cache_mgr: FUSECacheManager) -> None:
        """Test that reset_metrics clears all counters."""
        # Generate some metrics
        cache_mgr.cache_attr("/file.txt", {"st_mode": 0o100644})
        cache_mgr.get_attr("/file.txt")  # hit
        cache_mgr.get_attr("/other.txt")  # miss
        cache_mgr.invalidate_path("/file.txt")

        # Reset
        cache_mgr.reset_metrics()

        metrics = cache_mgr.get_metrics()
        assert metrics["attr_hits"] == 0
        assert metrics["attr_misses"] == 0
        assert metrics["invalidations"] == 0

    def test_reset_metrics_when_disabled(self, cache_mgr_no_metrics: FUSECacheManager) -> None:
        """Test that reset_metrics does nothing when metrics disabled."""
        # Should not raise any errors
        cache_mgr_no_metrics.reset_metrics()

    def test_metrics_hit_rate_with_zero_requests(self, cache_mgr: FUSECacheManager) -> None:
        """Test that hit rate is 0 when no requests made."""
        metrics = cache_mgr.get_metrics()
        assert metrics["attr_hit_rate"] == 0.0
        assert metrics["content_hit_rate"] == 0.0
        assert metrics["parsed_hit_rate"] == 0.0


class TestThreadSafety:
    """Test thread safety of cache operations."""

    def test_concurrent_attr_cache_operations(self, cache_mgr: FUSECacheManager) -> None:
        """Test that concurrent attribute cache operations are thread-safe."""

        def cache_and_get() -> None:
            for i in range(100):
                cache_mgr.cache_attr(f"/file{i}.txt", {"st_mode": 0o100644})
                cache_mgr.get_attr(f"/file{i}.txt")

        threads = [threading.Thread(target=cache_and_get) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not crash and metrics should be consistent
        metrics = cache_mgr.get_metrics()
        assert metrics["attr_hits"] + metrics["attr_misses"] == 500

    def test_concurrent_content_cache_operations(self, cache_mgr: FUSECacheManager) -> None:
        """Test that concurrent content cache operations are thread-safe."""

        def cache_and_get() -> None:
            for i in range(100):
                cache_mgr.cache_content(f"/file{i}.txt", f"content{i}".encode())
                cache_mgr.get_content(f"/file{i}.txt")

        threads = [threading.Thread(target=cache_and_get) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not crash
        metrics = cache_mgr.get_metrics()
        assert metrics["content_hits"] + metrics["content_misses"] == 500

    def test_concurrent_invalidation(self, cache_mgr: FUSECacheManager) -> None:
        """Test that concurrent invalidation is thread-safe."""
        # Pre-populate cache
        for i in range(100):
            cache_mgr.cache_attr(f"/file{i}.txt", {"st_mode": 0o100644})

        def invalidate() -> None:
            for i in range(100):
                cache_mgr.invalidate_path(f"/file{i}.txt")

        threads = [threading.Thread(target=invalidate) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not crash
        metrics = cache_mgr.get_metrics()
        assert metrics["invalidations"] == 500
