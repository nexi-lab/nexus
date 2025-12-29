"""Unit tests for ReBAC L1 cache implementation."""

import time

import pytest

from nexus.core.rebac_cache import ReBACPermissionCache


class TestReBACPermissionCache:
    """Test suite for in-memory L1 permission cache."""

    def test_cache_basic_operations(self):
        """Test basic get/set operations."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Test cache miss
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is None

        # Test cache set and hit
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is True

        # Test different permission on same subject/object
        result = cache.get("agent", "alice", "write", "file", "/doc.txt")
        assert result is None

    def test_cache_ttl_expiration(self):
        """Test that cache entries expire after TTL."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=1)  # 1 second TTL

        cache.set("agent", "alice", "read", "file", "/doc.txt", True)

        # Should hit immediately
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is True

        # Wait for expiration
        time.sleep(1.5)

        # Should miss after expiration
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is None

    def test_cache_invalidation_subject(self):
        """Test invalidating all entries for a subject."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries for alice
        cache.set("agent", "alice", "read", "file", "/doc1.txt", True)
        cache.set("agent", "alice", "write", "file", "/doc2.txt", True)
        cache.set("agent", "bob", "read", "file", "/doc3.txt", True)

        # Invalidate alice's entries
        count = cache.invalidate_subject("agent", "alice")
        assert count == 2

        # Alice's entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc1.txt") is None
        assert cache.get("agent", "alice", "write", "file", "/doc2.txt") is None

        # Bob's entry should still exist
        assert cache.get("agent", "bob", "read", "file", "/doc3.txt") is True

    def test_cache_invalidation_object(self):
        """Test invalidating all entries for an object."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries for same object
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.set("agent", "bob", "write", "file", "/doc.txt", True)
        cache.set("agent", "alice", "read", "file", "/other.txt", False)

        # Invalidate entries for /doc.txt
        count = cache.invalidate_object("file", "/doc.txt")
        assert count == 2

        # /doc.txt entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is None
        assert cache.get("agent", "bob", "write", "file", "/doc.txt") is None

        # /other.txt entry should still exist
        assert cache.get("agent", "alice", "read", "file", "/other.txt") is False

    def test_cache_invalidation_subject_object_pair(self):
        """Test precise invalidation for subject-object pair."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.set("agent", "alice", "write", "file", "/doc.txt", True)
        cache.set("agent", "alice", "read", "file", "/other.txt", True)
        cache.set("agent", "bob", "read", "file", "/doc.txt", True)

        # Invalidate only alice's entries for /doc.txt
        count = cache.invalidate_subject_object_pair("agent", "alice", "file", "/doc.txt")
        assert count == 2  # read and write permissions

        # Alice's entries for /doc.txt should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is None
        assert cache.get("agent", "alice", "write", "file", "/doc.txt") is None

        # Other entries should still exist
        assert cache.get("agent", "alice", "read", "file", "/other.txt") is True
        assert cache.get("agent", "bob", "read", "file", "/doc.txt") is True

    def test_cache_invalidation_prefix(self):
        """Test invalidating entries by object ID prefix."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries for files in different directories
        cache.set("agent", "alice", "read", "file", "/workspace/doc1.txt", True)
        cache.set("agent", "alice", "read", "file", "/workspace/doc2.txt", True)
        cache.set("agent", "alice", "read", "file", "/other/doc3.txt", True)

        # Invalidate all /workspace/* entries
        count = cache.invalidate_object_prefix("file", "/workspace/")
        assert count == 2

        # /workspace entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/workspace/doc1.txt") is None
        assert cache.get("agent", "alice", "read", "file", "/workspace/doc2.txt") is None

        # /other entry should still exist
        assert cache.get("agent", "alice", "read", "file", "/other/doc3.txt") is True

    def test_cache_metrics(self):
        """Test cache metrics tracking."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Perform operations
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # miss
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # hit
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # hit

        # Check stats
        stats = cache.get_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["sets"] == 1
        assert stats["total_requests"] == 3
        assert stats["hit_rate_percent"] == pytest.approx(66.67, rel=0.01)
        assert stats["avg_lookup_time_ms"] >= 0

    def test_cache_tenant_isolation(self):
        """Test that tenants are properly isolated."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries for different tenants
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, tenant_id="tenant1")
        cache.set("agent", "alice", "read", "file", "/doc.txt", False, tenant_id="tenant2")

        # Verify isolation
        result1 = cache.get("agent", "alice", "read", "file", "/doc.txt", tenant_id="tenant1")
        result2 = cache.get("agent", "alice", "read", "file", "/doc.txt", tenant_id="tenant2")

        assert result1 is True
        assert result2 is False

    def test_cache_write_tracking(self):
        """Test write frequency tracking for adaptive TTL."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_adaptive_ttl=True)

        # Track writes
        cache.track_write("/workspace/doc.txt")
        cache.track_write("/workspace/doc.txt")
        cache.track_write("/workspace/doc.txt")

        # Verify write frequency is tracked (internal state check)
        assert "/workspace/doc.txt" in cache._write_frequency
        count, _ = cache._write_frequency["/workspace/doc.txt"]
        assert count == 3

    def test_cache_clear(self):
        """Test clearing all cache entries."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Add entries
        cache.set("agent", "alice", "read", "file", "/doc1.txt", True)
        cache.set("agent", "bob", "write", "file", "/doc2.txt", False)

        stats = cache.get_stats()
        assert stats["current_size"] == 2

        # Clear cache
        cache.clear()

        stats = cache.get_stats()
        assert stats["current_size"] == 0
        assert cache.get("agent", "alice", "read", "file", "/doc1.txt") is None

    def test_cache_reset_stats(self):
        """Test resetting cache statistics."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Generate some metrics
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # miss
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # hit

        # Reset stats
        cache.reset_stats()

        # Verify reset
        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["sets"] == 0
        assert stats["total_requests"] == 0

        # Cache entries should still exist
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is True


class TestRevisionQuantization:
    """Test revision-based cache key quantization (Issue #909)."""

    def test_revision_bucket_calculation(self):
        """Test that revision buckets are calculated correctly."""
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: 25)  # Revision 25 -> bucket 2

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", "tenant1")
        assert ":r2" in key  # 25 // 10 = 2

    def test_revision_bucket_boundaries(self):
        """Test bucket boundaries work correctly."""
        test_cases = [
            (0, 0),   # 0 // 10 = 0
            (9, 0),   # 9 // 10 = 0
            (10, 1),  # 10 // 10 = 1
            (19, 1),  # 19 // 10 = 1
            (20, 2),  # 20 // 10 = 2
            (99, 9),  # 99 // 10 = 9
            (100, 10), # 100 // 10 = 10
        ]

        for revision, expected_bucket in test_cases:
            # Create fresh cache for each test to avoid local revision cache
            cache = ReBACPermissionCache(revision_quantization_window=10)
            cache.set_revision_fetcher(lambda t, r=revision: r)
            bucket = cache._get_revision_bucket("tenant1")
            assert bucket == expected_bucket, f"Revision {revision} -> expected bucket {expected_bucket}, got {bucket}"

    def test_cache_stable_within_window(self):
        """Test that cache entries are stable within a revision window."""
        current_revision = [20]  # Use list to allow mutation in closure
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: current_revision[0])

        # Set a value
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, "tenant1")

        # Advance revision within same bucket (20-29 all map to bucket 2)
        current_revision[0] = 25

        # Should still hit
        result = cache.get("agent", "alice", "read", "file", "/doc.txt", "tenant1")
        assert result is True

    def test_cache_miss_after_bucket_change(self):
        """Test that cache misses when revision bucket changes."""
        current_revision = [25]  # Bucket 2
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: current_revision[0])

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, "tenant1")

        # Advance to next bucket
        current_revision[0] = 30  # Bucket 3

        # Clear the local revision cache to simulate time passing (TTL expiry)
        cache._revision_cache.clear()

        # Should miss (different revision bucket in key)
        result = cache.get("agent", "alice", "read", "file", "/doc.txt", "tenant1")
        assert result is None

    def test_tenant_isolation_with_revisions(self):
        """Test that different tenants have independent revision tracking."""
        revisions = {"tenant1": 50, "tenant2": 100}
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: revisions.get(t, 0))

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, "tenant1")
        cache.set("agent", "alice", "read", "file", "/doc.txt", False, "tenant2")

        # Keys should differ due to different revision buckets
        assert cache.get("agent", "alice", "read", "file", "/doc.txt", "tenant1") is True
        assert cache.get("agent", "alice", "read", "file", "/doc.txt", "tenant2") is False

    def test_disabled_revision_quantization(self):
        """When revision quantization is disabled, always uses bucket 0."""
        cache = ReBACPermissionCache(
            revision_quantization_window=10,
            enable_revision_quantization=False
        )
        cache.set_revision_fetcher(lambda t: 999)

        bucket = cache._get_revision_bucket("tenant1")
        assert bucket == 0

    def test_fallback_without_fetcher(self):
        """Graceful degradation when fetcher not set."""
        cache = ReBACPermissionCache(revision_quantization_window=10)
        # Don't set fetcher

        bucket = cache._get_revision_bucket("tenant1")
        assert bucket == 0  # Fallback to 0

    def test_key_format_with_revision(self):
        """Cache key includes revision bucket with 'r' prefix."""
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: 35)

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", "tenant1")

        assert key == "agent:alice:read:file:/doc.txt:tenant1:r3"

    def test_revision_cache_local_caching(self):
        """Test that revisions are cached locally to reduce fetcher calls."""
        call_count = [0]

        def counting_fetcher(t):
            call_count[0] += 1
            return 50

        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(counting_fetcher)

        # First call fetches from callback
        cache._get_revision_bucket("tenant1")
        assert call_count[0] == 1

        # Second call should use local cache (no new fetch)
        cache._get_revision_bucket("tenant1")
        assert call_count[0] == 1

    def test_stats_include_revision_info(self):
        """Test that stats include revision quantization configuration."""
        cache = ReBACPermissionCache(
            revision_quantization_window=15,
            enable_revision_quantization=True
        )

        stats = cache.get_stats()
        assert stats["revision_quantization_window"] == 15
        assert stats["enable_revision_quantization"] is True

    def test_deprecation_warning_for_old_param(self):
        """Test that using old quantization_interval triggers deprecation warning."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ReBACPermissionCache(quantization_interval=5)

            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "quantization_interval is deprecated" in str(w[0].message)
