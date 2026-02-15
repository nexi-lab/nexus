"""Comprehensive tests for DirectoryVisibilityCache.

Tests cover:
- Initialization with default and custom parameters
- Cache hit/miss behavior
- TTL expiration and cleanup
- Eviction at max_entries capacity
- Invalidation with various filters
- Bitmap computation with Tiger Cache
- Metrics tracking
- Thread safety
- Clear and len operations
"""

import time
from threading import Thread
from unittest.mock import MagicMock

from nexus.services.permissions.dir_visibility_cache import (
    DirectoryVisibilityCache,
)


class TestInitialization:
    """Test DirectoryVisibilityCache initialization."""

    def test_default_initialization(self):
        """Test cache initialized with default parameters."""
        cache = DirectoryVisibilityCache()

        assert cache._tiger_cache is None
        assert cache._ttl == 300
        assert cache._max_entries == 10000
        assert len(cache) == 0
        assert cache._hits == 0
        assert cache._misses == 0
        assert cache._bitmap_computes == 0

    def test_custom_initialization(self):
        """Test cache initialized with custom parameters."""
        tiger_cache = MagicMock()
        cache = DirectoryVisibilityCache(
            tiger_cache=tiger_cache,
            ttl=600,
            max_entries=5000,
        )

        assert cache._tiger_cache is tiger_cache
        assert cache._ttl == 600
        assert cache._max_entries == 5000
        assert len(cache) == 0

    def test_initialization_with_none_tiger_cache(self):
        """Test cache can be initialized with explicit None for tiger_cache."""
        cache = DirectoryVisibilityCache(tiger_cache=None, ttl=120)

        assert cache._tiger_cache is None
        assert cache._ttl == 120


class TestCacheHitMiss:
    """Test cache hit and miss behavior."""

    def test_miss_returns_none(self):
        """Test cache miss returns None."""
        cache = DirectoryVisibilityCache()

        result = cache.is_visible("zone1", "user", "alice", "/workspace")

        assert result is None
        assert cache._misses == 1
        assert cache._hits == 0

    def test_set_then_get_returns_correct_value(self):
        """Test setting then getting returns the cached value."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True, "test_reason")
        result = cache.is_visible("zone1", "user", "alice", "/workspace")

        assert result is True
        assert cache._hits == 1
        assert cache._misses == 0

    def test_different_keys_dont_interfere(self):
        """Test that different cache keys don't interfere with each other."""
        cache = DirectoryVisibilityCache()

        # Set visibility for different keys
        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.set_visible("zone1", "user", "bob", "/workspace", False)
        cache.set_visible("zone2", "user", "alice", "/workspace", True)
        cache.set_visible("zone1", "user", "alice", "/data", False)

        # Verify each key returns correct value
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is True
        assert cache.is_visible("zone1", "user", "bob", "/workspace") is False
        assert cache.is_visible("zone2", "user", "alice", "/workspace") is True
        assert cache.is_visible("zone1", "user", "alice", "/data") is False

    def test_true_and_false_values_both_cached(self):
        """Test that both True and False visibility values are cached correctly."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/visible", True)
        cache.set_visible("zone1", "user", "alice", "/invisible", False)

        assert cache.is_visible("zone1", "user", "alice", "/visible") is True
        assert cache.is_visible("zone1", "user", "alice", "/invisible") is False

    def test_reason_is_stored_correctly(self):
        """Test that visibility reason is stored and retrievable."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True, "has_descendant")

        # Access the cached entry directly to verify reason
        key = ("zone1", "user", "alice", "/workspace")
        entry = cache._cache[key]

        assert entry.reason == "has_descendant"
        assert entry.visible is True

    def test_empty_reason_default(self):
        """Test that empty reason defaults to empty string."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)

        key = ("zone1", "user", "alice", "/workspace")
        entry = cache._cache[key]

        assert entry.reason == ""


class TestTTLExpiration:
    """Test TTL expiration behavior."""

    def test_entry_within_ttl_returns_value(self):
        """Test that entry within TTL returns cached value."""
        cache = DirectoryVisibilityCache(ttl=300)

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        result = cache.is_visible("zone1", "user", "alice", "/workspace")

        assert result is True
        assert cache._hits == 1

    def test_entry_past_ttl_returns_none(self, monkeypatch):
        """Test that entry past TTL returns None (cache miss)."""
        cache = DirectoryVisibilityCache(ttl=60)

        # Mock time.time to control timestamps
        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Set entry at t=1000
        cache.set_visible("zone1", "user", "alice", "/workspace", True)

        # Advance time beyond TTL (60 seconds)
        monkeypatch.setattr(time, "time", lambda: current_time + 61)

        result = cache.is_visible("zone1", "user", "alice", "/workspace")

        assert result is None
        assert cache._misses == 1
        assert cache._hits == 0

    def test_expired_entry_is_removed_from_cache(self, monkeypatch):
        """Test that expired entry is removed from cache after check."""
        cache = DirectoryVisibilityCache(ttl=60)

        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Set entry
        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        assert len(cache) == 1

        # Advance time beyond TTL
        monkeypatch.setattr(time, "time", lambda: current_time + 61)

        # Access expired entry
        cache.is_visible("zone1", "user", "alice", "/workspace")

        # Entry should be removed
        assert len(cache) == 0

    def test_multiple_entries_ttl_expiration(self, monkeypatch):
        """Test that multiple entries expire independently based on TTL."""
        cache = DirectoryVisibilityCache(ttl=60)

        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Set first entry at t=1000
        cache.set_visible("zone1", "user", "alice", "/workspace", True)

        # Advance time and set second entry at t=1030
        monkeypatch.setattr(time, "time", lambda: current_time + 30)
        cache.set_visible("zone1", "user", "bob", "/data", False)

        # At t=1050, first entry is still valid (50s old), second is valid (20s old)
        monkeypatch.setattr(time, "time", lambda: current_time + 50)
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is True
        assert cache.is_visible("zone1", "user", "bob", "/data") is False

        # At t=1070, first entry expired (70s old), second still valid (40s old)
        monkeypatch.setattr(time, "time", lambda: current_time + 70)
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is None
        assert cache.is_visible("zone1", "user", "bob", "/data") is False


class TestEviction:
    """Test cache eviction at max_entries capacity."""

    def test_eviction_at_max_entries_capacity(self, monkeypatch):
        """Test that eviction occurs when reaching max_entries."""
        cache = DirectoryVisibilityCache(max_entries=10)

        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Fill cache to capacity
        for i in range(10):
            cache.set_visible("zone1", "user", f"user{i}", "/workspace", True)
            current_time += 1
            monkeypatch.setattr(time, "time", lambda t=current_time: t)

        assert len(cache) == 10

        # Add one more entry - should trigger eviction
        cache.set_visible("zone1", "user", "user10", "/workspace", True)

        # Cache should evict 10% (1 entry) and add new one
        assert len(cache) == 10

    def test_evicts_oldest_10_percent(self, monkeypatch):
        """Test that eviction removes oldest 10% of entries."""
        cache = DirectoryVisibilityCache(max_entries=10)

        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Add entries with incrementing timestamps
        for i in range(10):
            cache.set_visible("zone1", "user", f"user{i}", "/workspace", True)
            current_time += 1
            monkeypatch.setattr(time, "time", lambda t=current_time: t)

        # Add 11th entry
        cache.set_visible("zone1", "user", "user10", "/workspace", True)

        # Oldest entry (user0) should be evicted
        assert cache.is_visible("zone1", "user", "user0", "/workspace") is None
        # Newer entries should still exist
        assert cache.is_visible("zone1", "user", "user9", "/workspace") is True
        assert cache.is_visible("zone1", "user", "user10", "/workspace") is True

    def test_new_entry_added_after_eviction(self, monkeypatch):
        """Test that new entry is added after eviction."""
        cache = DirectoryVisibilityCache(max_entries=5)

        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Fill to capacity
        for i in range(5):
            cache.set_visible("zone1", "user", f"user{i}", "/workspace", True)
            current_time += 1
            monkeypatch.setattr(time, "time", lambda t=current_time: t)

        # Add new entry
        cache.set_visible("zone1", "user", "new_user", "/workspace", True)

        # New entry should exist
        assert cache.is_visible("zone1", "user", "new_user", "/workspace") is True

    def test_small_cache_eviction_behavior(self, monkeypatch):
        """Test eviction behavior with small cache (max_entries=5)."""
        cache = DirectoryVisibilityCache(max_entries=5)

        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        # Fill cache
        for i in range(5):
            cache.set_visible("zone1", "user", f"user{i}", "/workspace", True)
            current_time += 1
            monkeypatch.setattr(time, "time", lambda t=current_time: t)

        # Trigger eviction - should evict max(1, 5//10) = 1 entry
        cache.set_visible("zone1", "user", "user5", "/workspace", True)

        assert len(cache) == 5  # 5 - 1 (evicted) + 1 (new) = 5

    def test_evict_oldest_with_empty_cache(self):
        """Test that _evict_oldest handles empty cache gracefully."""
        cache = DirectoryVisibilityCache()

        # Call _evict_oldest on empty cache - should not crash
        cache._evict_oldest()

        assert len(cache) == 0


class TestInvalidation:
    """Test cache invalidation with various filters."""

    def test_invalidate_with_zone_id_filter(self):
        """Test invalidation filtered by zone_id."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.set_visible("zone2", "user", "alice", "/workspace", True)
        cache.set_visible("zone1", "user", "bob", "/data", False)

        count = cache.invalidate(zone_id="zone1")

        assert count == 2
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is None
        assert cache.is_visible("zone1", "user", "bob", "/data") is None
        assert cache.is_visible("zone2", "user", "alice", "/workspace") is True

    def test_invalidate_with_subject_filter(self):
        """Test invalidation filtered by subject type and ID."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.set_visible("zone1", "user", "bob", "/workspace", False)
        cache.set_visible("zone1", "agent", "alice", "/workspace", True)

        count = cache.invalidate(subject_type="user", subject_id="alice")

        assert count == 1
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is None
        assert cache.is_visible("zone1", "user", "bob", "/workspace") is False
        assert cache.is_visible("zone1", "agent", "alice", "/workspace") is True

    def test_invalidate_with_dir_path_invalidates_ancestors(self):
        """Test that invalidating a path also invalidates ancestor paths."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/", True)
        cache.set_visible("zone1", "user", "alice", "/a", True)
        cache.set_visible("zone1", "user", "alice", "/a/b", True)
        cache.set_visible("zone1", "user", "alice", "/a/b/c", True)
        cache.set_visible("zone1", "user", "alice", "/x/y", True)

        # Invalidate /a/b/c - should invalidate /a/b, /a, and / (ancestors)
        cache.invalidate(dir_path="/a/b/c/file.txt")

        # Should invalidate /, /a, /a/b (ancestors of /a/b/c)
        # /a/b/c itself matches as well since normalized_dir starts with normalized_k
        assert cache.is_visible("zone1", "user", "alice", "/") is None
        assert cache.is_visible("zone1", "user", "alice", "/a") is None
        assert cache.is_visible("zone1", "user", "alice", "/a/b") is None
        assert cache.is_visible("zone1", "user", "alice", "/a/b/c") is None
        # Unrelated path should remain
        assert cache.is_visible("zone1", "user", "alice", "/x/y") is True

    def test_invalidate_no_filters_clears_all(self):
        """Test that invalidate with no filters clears all entries."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.set_visible("zone2", "user", "bob", "/data", False)
        cache.set_visible("zone1", "agent", "agent1", "/tmp", True)

        count = cache.invalidate()

        assert count == 3
        assert len(cache) == 0

    def test_invalidate_returns_count(self):
        """Test that invalidate returns the number of invalidated entries."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.set_visible("zone1", "user", "bob", "/workspace", False)

        count = cache.invalidate(zone_id="zone1")

        assert count == 2

    def test_invalidate_no_matches_returns_zero(self):
        """Test that invalidate returns 0 when no entries match."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)

        count = cache.invalidate(zone_id="zone_nonexistent")

        assert count == 0
        assert len(cache) == 1

    def test_invalidate_for_resource_invalidates_ancestor_paths(self):
        """Test invalidate_for_resource invalidates all ancestor paths."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/", True)
        cache.set_visible("zone1", "user", "alice", "/a", True)
        cache.set_visible("zone1", "user", "alice", "/a/b", True)
        cache.set_visible("zone1", "user", "bob", "/x", True)

        cache.invalidate_for_resource("/a/b/c/file.txt", "zone1")

        # Should invalidate /a/b, /a, / for zone1
        assert cache.is_visible("zone1", "user", "alice", "/") is None
        assert cache.is_visible("zone1", "user", "alice", "/a") is None
        assert cache.is_visible("zone1", "user", "alice", "/a/b") is None
        # Unrelated path in same zone should remain
        assert cache.is_visible("zone1", "user", "bob", "/x") is True

    def test_get_ancestor_paths_computation(self):
        """Test _get_ancestor_paths returns correct ancestor sequence."""
        cache = DirectoryVisibilityCache()

        # Test /a/b/c
        ancestors = cache._get_ancestor_paths("/a/b/c")
        assert ancestors == ["/a/b", "/a", "/"]

        # Test /a/b
        ancestors = cache._get_ancestor_paths("/a/b")
        assert ancestors == ["/a", "/"]

        # Test /a
        ancestors = cache._get_ancestor_paths("/a")
        assert ancestors == ["/"]

        # Test root
        ancestors = cache._get_ancestor_paths("/")
        assert ancestors == ["/"]

        # Test trailing slash
        ancestors = cache._get_ancestor_paths("/a/b/c/")
        assert ancestors == ["/a/b", "/a", "/"]


class TestBitmapComputation:
    """Test bitmap-based visibility computation with Tiger Cache."""

    def test_returns_none_when_tiger_cache_is_none(self):
        """Test compute_from_tiger_bitmap returns None when tiger_cache is None."""
        cache = DirectoryVisibilityCache(tiger_cache=None)

        result = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")

        assert result is None
        assert cache._bitmap_computes == 0

    def test_returns_false_when_no_accessible_resources(self):
        """Test returns False when user has no accessible resources."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = []

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        result = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")

        assert result is False
        assert cache._bitmap_computes == 1
        # Should be cached
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is False

    def test_returns_true_when_descendant_found(self):
        """Test returns True when accessible descendant is found."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = [101, 102]
        tiger_cache._resource_map.get_resource_id.side_effect = [
            ("file", "/workspace/data/file1.txt"),
            ("file", "/other/file2.txt"),
        ]

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        result = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")

        assert result is True
        assert cache._bitmap_computes == 1
        # Should be cached with reason
        key = ("zone1", "user", "alice", "/workspace")
        entry = cache._cache[key]
        assert entry.visible is True
        assert entry.reason == "descendant:/workspace/data/file1.txt"

    def test_caches_result_after_computation(self):
        """Test that bitmap computation result is cached."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = [101]
        tiger_cache._resource_map.get_resource_id.return_value = ("file", "/workspace/file.txt")

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        # First call computes
        result1 = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")
        assert result1 is True
        assert cache._bitmap_computes == 1

        # Second call hits cache (doesn't increment bitmap_computes)
        result2 = cache.is_visible("zone1", "user", "alice", "/workspace")
        assert result2 is True
        assert cache._bitmap_computes == 1
        assert cache._hits == 1

    def test_root_path_handling(self):
        """Test that root path '/' is handled correctly in bitmap computation."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = [101]
        tiger_cache._resource_map.get_resource_id.return_value = ("file", "/workspace/file.txt")

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        result = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/")

        # Root should match any path
        assert result is True

    def test_exact_path_match(self):
        """Test that exact path match (not just prefix) is detected."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = [101]
        tiger_cache._resource_map.get_resource_id.return_value = ("file", "/workspace")

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        result = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")

        # Exact match should return True
        assert result is True

    def test_no_descendants_caches_false(self):
        """Test that finding no descendants caches False result."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = [101, 102]
        tiger_cache._resource_map.get_resource_id.side_effect = [
            ("file", "/other/file1.txt"),
            ("file", "/different/file2.txt"),
        ]

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        result = cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")

        assert result is False
        # Should be cached
        key = ("zone1", "user", "alice", "/workspace")
        entry = cache._cache[key]
        assert entry.visible is False
        assert entry.reason == "no_descendants_in_bitmap"


class TestMetrics:
    """Test cache metrics tracking."""

    def test_initial_metrics_zeros(self):
        """Test that initial metrics are all zeros."""
        cache = DirectoryVisibilityCache(ttl=300, max_entries=1000)

        metrics = cache.get_metrics()

        assert metrics["hits"] == 0
        assert metrics["misses"] == 0
        assert metrics["hit_rate"] == 0.0
        assert metrics["bitmap_computes"] == 0
        assert metrics["cache_size"] == 0
        assert metrics["max_entries"] == 1000
        assert metrics["ttl"] == 300

    def test_hits_and_misses_tracked(self):
        """Test that hits and misses are tracked correctly."""
        cache = DirectoryVisibilityCache()

        # Miss
        cache.is_visible("zone1", "user", "alice", "/workspace")

        # Hit
        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.is_visible("zone1", "user", "alice", "/workspace")

        # Another miss
        cache.is_visible("zone1", "user", "bob", "/data")

        metrics = cache.get_metrics()

        assert metrics["hits"] == 1
        assert metrics["misses"] == 2
        assert metrics["cache_size"] == 1

    def test_hit_rate_calculation(self):
        """Test that hit rate is calculated correctly."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)

        # 3 hits
        cache.is_visible("zone1", "user", "alice", "/workspace")
        cache.is_visible("zone1", "user", "alice", "/workspace")
        cache.is_visible("zone1", "user", "alice", "/workspace")

        # 1 miss
        cache.is_visible("zone1", "user", "bob", "/data")

        metrics = cache.get_metrics()

        assert metrics["hits"] == 3
        assert metrics["misses"] == 1
        assert metrics["hit_rate"] == 0.75  # 3 / (3 + 1)

    def test_bitmap_computes_tracked(self):
        """Test that bitmap computations are tracked."""
        tiger_cache = MagicMock()
        tiger_cache.get_accessible_resources.return_value = []

        cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)

        cache.compute_from_tiger_bitmap("zone1", "user", "alice", "/workspace")
        cache.compute_from_tiger_bitmap("zone1", "user", "bob", "/data")

        metrics = cache.get_metrics()

        assert metrics["bitmap_computes"] == 2


class TestThreadSafety:
    """Test thread safety of cache operations."""

    def test_concurrent_set_visible(self):
        """Test concurrent set_visible operations are thread-safe."""
        cache = DirectoryVisibilityCache()

        def set_entries(user_prefix, count):
            for i in range(count):
                cache.set_visible("zone1", "user", f"{user_prefix}{i}", "/workspace", True)

        threads = [
            Thread(target=set_entries, args=("user_a_", 50)),
            Thread(target=set_entries, args=("user_b_", 50)),
            Thread(target=set_entries, args=("user_c_", 50)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 150 entries should be present
        assert len(cache) == 150

    def test_concurrent_is_visible_and_invalidate(self):
        """Test concurrent is_visible and invalidate operations are thread-safe."""
        cache = DirectoryVisibilityCache()

        # Pre-populate cache
        for i in range(100):
            cache.set_visible("zone1", "user", f"user{i}", "/workspace", True)

        results = []

        def read_entries():
            for i in range(100):
                result = cache.is_visible("zone1", "user", f"user{i}", "/workspace")
                results.append(result)

        def invalidate_entries():
            cache.invalidate(zone_id="zone1")

        threads = [
            Thread(target=read_entries),
            Thread(target=invalidate_entries),
            Thread(target=read_entries),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not crash - results may be mixed (True, False, None)
        assert len(results) == 200


class TestClearAndLen:
    """Test clear() and __len__() operations."""

    def test_clear_empties_cache(self):
        """Test that clear() removes all entries."""
        cache = DirectoryVisibilityCache()

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        cache.set_visible("zone1", "user", "bob", "/data", False)
        cache.set_visible("zone2", "user", "alice", "/tmp", True)

        assert len(cache) == 3

        cache.clear()

        assert len(cache) == 0
        assert cache.is_visible("zone1", "user", "alice", "/workspace") is None

    def test_len_returns_correct_count(self):
        """Test that __len__ returns correct number of entries."""
        cache = DirectoryVisibilityCache()

        assert len(cache) == 0

        cache.set_visible("zone1", "user", "alice", "/workspace", True)
        assert len(cache) == 1

        cache.set_visible("zone1", "user", "bob", "/data", False)
        assert len(cache) == 2

        cache.invalidate(subject_id="alice")
        assert len(cache) == 1

        cache.clear()
        assert len(cache) == 0
