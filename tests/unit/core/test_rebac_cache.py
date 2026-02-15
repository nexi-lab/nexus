"""Unit tests for ReBAC L1 cache implementation."""

import time

import pytest

from nexus.rebac.rebac_cache import ReBACPermissionCache


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

    def test_cache_zone_isolation(self):
        """Test that zones are properly isolated."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries for different zones
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, zone_id="zone1")
        cache.set("agent", "alice", "read", "file", "/doc.txt", False, zone_id="zone2")

        # Verify isolation
        result1 = cache.get("agent", "alice", "read", "file", "/doc.txt", zone_id="zone1")
        result2 = cache.get("agent", "alice", "read", "file", "/doc.txt", zone_id="zone2")

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

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", "zone1")
        assert ":r2" in key  # 25 // 10 = 2

    def test_revision_bucket_boundaries(self):
        """Test bucket boundaries work correctly."""
        test_cases = [
            (0, 0),  # 0 // 10 = 0
            (9, 0),  # 9 // 10 = 0
            (10, 1),  # 10 // 10 = 1
            (19, 1),  # 19 // 10 = 1
            (20, 2),  # 20 // 10 = 2
            (99, 9),  # 99 // 10 = 9
            (100, 10),  # 100 // 10 = 10
        ]

        for revision, expected_bucket in test_cases:
            # Create fresh cache for each test to avoid local revision cache
            cache = ReBACPermissionCache(revision_quantization_window=10)
            cache.set_revision_fetcher(lambda t, r=revision: r)
            bucket = cache._get_revision_bucket("zone1")
            assert bucket == expected_bucket, (
                f"Revision {revision} -> expected bucket {expected_bucket}, got {bucket}"
            )

    def test_cache_stable_within_window(self):
        """Test that cache entries are stable within a revision window."""
        current_revision = [20]  # Use list to allow mutation in closure
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: current_revision[0])

        # Set a value
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, "zone1")

        # Advance revision within same bucket (20-29 all map to bucket 2)
        current_revision[0] = 25

        # Should still hit
        result = cache.get("agent", "alice", "read", "file", "/doc.txt", "zone1")
        assert result is True

    def test_cache_miss_after_bucket_change(self):
        """Test that cache misses when revision bucket changes."""
        current_revision = [25]  # Bucket 2
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: current_revision[0])

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, "zone1")

        # Advance to next bucket
        current_revision[0] = 30  # Bucket 3

        # Clear the local revision cache to simulate time passing (TTL expiry)
        cache._revision_cache.clear()

        # Should miss (different revision bucket in key)
        result = cache.get("agent", "alice", "read", "file", "/doc.txt", "zone1")
        assert result is None

    def test_zone_isolation_with_revisions(self):
        """Test that different zones have independent revision tracking."""
        revisions = {"zone1": 50, "zone2": 100}
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: revisions.get(t, 0))

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, "zone1")
        cache.set("agent", "alice", "read", "file", "/doc.txt", False, "zone2")

        # Keys should differ due to different revision buckets
        assert cache.get("agent", "alice", "read", "file", "/doc.txt", "zone1") is True
        assert cache.get("agent", "alice", "read", "file", "/doc.txt", "zone2") is False

    def test_disabled_revision_quantization(self):
        """When revision quantization is disabled, always uses bucket 0."""
        cache = ReBACPermissionCache(
            revision_quantization_window=10, enable_revision_quantization=False
        )
        cache.set_revision_fetcher(lambda t: 999)

        bucket = cache._get_revision_bucket("zone1")
        assert bucket == 0

    def test_fallback_without_fetcher(self):
        """Graceful degradation when fetcher not set."""
        cache = ReBACPermissionCache(revision_quantization_window=10)
        # Don't set fetcher

        bucket = cache._get_revision_bucket("zone1")
        assert bucket == 0  # Fallback to 0

    def test_key_format_with_revision(self):
        """Cache key includes revision bucket with 'r' prefix."""
        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(lambda t: 35)

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", "zone1")

        assert key == "agent:alice:read:file:/doc.txt:zone1:r3"

    def test_revision_cache_local_caching(self):
        """Test that revisions are cached locally to reduce fetcher calls."""
        call_count = [0]

        def counting_fetcher(t):
            call_count[0] += 1
            return 50

        cache = ReBACPermissionCache(revision_quantization_window=10)
        cache.set_revision_fetcher(counting_fetcher)

        # First call fetches from callback
        cache._get_revision_bucket("zone1")
        assert call_count[0] == 1

        # Second call should use local cache (no new fetch)
        cache._get_revision_bucket("zone1")
        assert call_count[0] == 1

    def test_stats_include_revision_info(self):
        """Test that stats include revision quantization configuration."""
        cache = ReBACPermissionCache(
            revision_quantization_window=15, enable_revision_quantization=True
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

            # Filter for the specific deprecation warning we're testing
            deprecation_warnings = [
                warning
                for warning in w
                if issubclass(warning.category, DeprecationWarning)
                and "quantization_interval is deprecated" in str(warning.message)
            ]
            assert len(deprecation_warnings) >= 1, (
                f"Expected at least 1 deprecation warning about quantization_interval, "
                f"got {len(deprecation_warnings)} (total warnings: {len(w)})"
            )


class TestXFetchAlgorithm:
    """Test XFetch probabilistic early expiration algorithm (Issue #718).

    Based on VLDB 2015 paper: "Optimal Probabilistic Cache Stampede Prevention"
    https://www.vldb.org/pvldb/vol8/p886-vattani.pdf
    """

    def test_xfetch_default_beta(self):
        """Test that default beta is 1.0."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)
        stats = cache.get_stats()
        assert stats["xfetch_beta"] == 1.0

    def test_xfetch_custom_beta(self):
        """Test custom beta configuration."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, xfetch_beta=2.0)
        stats = cache.get_stats()
        assert stats["xfetch_beta"] == 2.0

    def test_set_with_delta(self):
        """Test that delta is stored in entry metadata."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set with delta
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, delta=0.05)

        # Verify metadata includes delta
        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        metadata = cache._entry_metadata.get(key)
        assert metadata is not None
        assert len(metadata) == 4  # (created_at, jittered_ttl, delta, revision)
        assert metadata[2] == 0.05  # delta

    def test_set_default_delta_zero(self):
        """Test that default delta is 0.0."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        cache.set("agent", "alice", "read", "file", "/doc.txt", True)

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        metadata = cache._entry_metadata.get(key)
        assert metadata is not None
        assert metadata[2] == 0.0  # default delta

    def test_xfetch_expired_returns_true(self):
        """Test that expired entries always return True for refresh."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=1)

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, delta=0.01)

        # Wait for expiration
        time.sleep(1.5)

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        assert cache._should_refresh_xfetch(key) is True

    def test_xfetch_fresh_entry_low_probability(self):
        """Test that fresh entries have low refresh probability."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Set with small delta - fresh entry shouldn't trigger refresh often
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, delta=0.01)

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)

        # Run many iterations - fresh entry with small delta should rarely trigger
        refresh_count = 0
        iterations = 1000
        for _ in range(iterations):
            if cache._should_refresh_xfetch(key):
                refresh_count += 1

        # With 300s TTL and 0.01s delta, probability should be very low
        # Expect < 5% refresh rate for fresh entries
        refresh_rate = refresh_count / iterations
        assert refresh_rate < 0.05, f"Fresh entry refresh rate too high: {refresh_rate}"

    def test_xfetch_higher_delta_more_aggressive(self):
        """Test that higher delta leads to earlier refresh."""
        # Create two caches with same TTL
        cache1 = ReBACPermissionCache(max_size=100, ttl_seconds=60)
        cache2 = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries with different deltas
        # For XFetch to trigger, delta * beta * -log(random) must >= time_remaining
        # With 2 seconds remaining and beta=1.0, we need delta comparable to 2s
        cache1.set("agent", "alice", "read", "file", "/doc.txt", True, delta=0.5)  # 500ms
        cache2.set("agent", "alice", "read", "file", "/doc.txt", True, delta=5.0)  # 5s

        key1 = cache1._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        key2 = cache2._make_key("agent", "alice", "read", "file", "/doc.txt", None)

        # Simulate entry that is 58 seconds old (2 seconds remaining)
        now = time.time()
        cache1._entry_metadata[key1] = (now - 58, 60.0, 0.5, 0)
        cache2._entry_metadata[key2] = (now - 58, 60.0, 5.0, 0)

        # Run many iterations
        refresh_count1 = 0
        refresh_count2 = 0
        iterations = 1000
        for _ in range(iterations):
            if cache1._should_refresh_xfetch(key1):
                refresh_count1 += 1
            if cache2._should_refresh_xfetch(key2):
                refresh_count2 += 1

        # Higher delta should have significantly more refreshes
        assert refresh_count2 > refresh_count1, (
            f"Higher delta should refresh more: low={refresh_count1}, high={refresh_count2}"
        )

    def test_xfetch_higher_beta_more_aggressive(self):
        """Test that higher beta leads to more aggressive refresh."""
        cache1 = ReBACPermissionCache(max_size=100, ttl_seconds=60, xfetch_beta=0.5)
        cache2 = ReBACPermissionCache(max_size=100, ttl_seconds=60, xfetch_beta=5.0)

        # Set entries with same delta
        # With 2 seconds remaining and delta=2.0, higher beta will trigger more
        cache1.set("agent", "alice", "read", "file", "/doc.txt", True, delta=2.0)
        cache2.set("agent", "alice", "read", "file", "/doc.txt", True, delta=2.0)

        key1 = cache1._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        key2 = cache2._make_key("agent", "alice", "read", "file", "/doc.txt", None)

        # Simulate entry that is 58 seconds old (2 seconds remaining)
        now = time.time()
        cache1._entry_metadata[key1] = (now - 58, 60.0, 2.0, 0)
        cache2._entry_metadata[key2] = (now - 58, 60.0, 2.0, 0)

        # Run many iterations
        refresh_count1 = 0
        refresh_count2 = 0
        iterations = 1000
        for _ in range(iterations):
            if cache1._should_refresh_xfetch(key1):
                refresh_count1 += 1
            if cache2._should_refresh_xfetch(key2):
                refresh_count2 += 1

        # Higher beta should have more refreshes
        assert refresh_count2 > refresh_count1, (
            f"Higher beta should refresh more: beta=0.5 had {refresh_count1}, beta=5.0 had {refresh_count2}"
        )

    def test_xfetch_fallback_for_zero_delta(self):
        """Test that zero delta falls back to refresh-ahead threshold."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, refresh_ahead_factor=0.7)

        # Set entry without delta
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)  # delta=0.0

        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)

        # Before refresh threshold (70% of TTL = 42 seconds)
        now = time.time()
        cache._entry_metadata[key] = (now - 30, 60.0, 0.0, 0)  # 30s old
        assert cache._should_refresh_xfetch(key) is False

        # After refresh threshold
        cache._entry_metadata[key] = (now - 45, 60.0, 0.0, 0)  # 45s old
        assert cache._should_refresh_xfetch(key) is True

    def test_get_with_refresh_check_tracks_xfetch(self):
        """Test that get_with_refresh_check uses XFetch and tracks metrics."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Set entry with large delta (5 seconds)
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, delta=5.0)

        # Get the key immediately after set (before any time passes)
        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)

        # Simulate passage of time near expiry (only 2 seconds remaining)
        # With delta=5.0 and beta=1.0, E[refresh_factor] = 5.0
        # So with 2 seconds remaining, we should trigger frequently
        now = time.time()
        cache._entry_metadata[key] = (now - 58, 60.0, 5.0, 0)  # 58s old, 2s remaining

        # Should trigger refresh at least sometimes
        refresh_triggered = False
        for _ in range(100):
            result, needs_refresh, _ = cache.get_with_refresh_check(
                "agent", "alice", "read", "file", "/doc.txt"
            )
            if needs_refresh:
                refresh_triggered = True
                break

        # Near expiry with large delta should trigger at least once
        assert refresh_triggered, "XFetch should trigger near expiry with large delta"

    def test_release_compute_with_delta(self):
        """Test that release_compute accepts and uses delta."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Acquire compute
        should_compute, key = cache.try_acquire_compute(
            "agent", "alice", "read", "file", "/doc.txt"
        )
        assert should_compute is True

        # Release with delta
        cache.release_compute(
            key, True, "agent", "alice", "read", "file", "/doc.txt", None, delta=0.1
        )

        # Verify delta was stored
        metadata = cache._entry_metadata.get(key)
        assert metadata is not None
        assert metadata[2] == 0.1

    def test_xfetch_metrics_in_stats(self):
        """Test that XFetch metrics are included in stats."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        stats = cache.get_stats()
        assert "xfetch_beta" in stats
        assert "xfetch_early_refreshes" in stats
        assert stats["xfetch_early_refreshes"] == 0

    def test_xfetch_metrics_reset(self):
        """Test that reset_stats resets XFetch metrics."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Force increment (simulate some early refreshes)
        cache._xfetch_early_refreshes = 5

        cache.reset_stats()

        stats = cache.get_stats()
        assert stats["xfetch_early_refreshes"] == 0

    def test_public_should_refresh_xfetch(self):
        """Test public should_refresh_xfetch method."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, delta=0.05)

        # Should work without errors
        result = cache.should_refresh_xfetch("agent", "alice", "read", "file", "/doc.txt")
        assert isinstance(result, bool)

    def test_public_should_refresh_xfetch_with_beta_override(self):
        """Test public should_refresh_xfetch with beta override."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, xfetch_beta=1.0)

        cache.set("agent", "alice", "read", "file", "/doc.txt", True, delta=0.1)

        # Simulate near expiry
        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        now = time.time()
        cache._entry_metadata[key] = (now - 55, 60.0, 0.1, 0)

        # Run many iterations with different betas
        refresh_beta_low = 0
        refresh_beta_high = 0
        iterations = 500

        for _ in range(iterations):
            if cache.should_refresh_xfetch("agent", "alice", "read", "file", "/doc.txt", beta=0.1):
                refresh_beta_low += 1
            if cache.should_refresh_xfetch("agent", "alice", "read", "file", "/doc.txt", beta=5.0):
                refresh_beta_high += 1

        # Higher beta should trigger more refreshes
        assert refresh_beta_high >= refresh_beta_low


class TestIssue1077TieredTTL:
    """Test suite for Issue #1077: Tiered TTL by relation type."""

    def test_tiered_ttl_config_default(self):
        """Test default tiered TTL configuration."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Check default tiered TTL config exists
        assert "owner" in cache._tiered_ttl_config
        assert "editor" in cache._tiered_ttl_config
        assert "viewer" in cache._tiered_ttl_config
        assert "inherited" in cache._tiered_ttl_config
        assert "denial" in cache._tiered_ttl_config

        # Owner should have longer TTL (1 hour)
        assert cache._tiered_ttl_config["owner"] == 3600

        # Editor/viewer should have medium TTL (10 min)
        assert cache._tiered_ttl_config["editor"] == 600
        assert cache._tiered_ttl_config["viewer"] == 600

        # Inherited should be shorter (5 min)
        assert cache._tiered_ttl_config["inherited"] == 300

        # Denial should be shortest (1 min)
        assert cache._tiered_ttl_config["denial"] == 60

    def test_tiered_ttl_custom_config(self):
        """Test custom tiered TTL configuration."""
        custom_config = {
            "owner": 7200,  # 2 hours
            "editor": 1200,  # 20 min
            "viewer": 900,  # 15 min
        }
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300, tiered_ttl_config=custom_config)

        assert cache._tiered_ttl_config["owner"] == 7200
        assert cache._tiered_ttl_config["editor"] == 1200
        assert cache._tiered_ttl_config["viewer"] == 900

    def test_get_ttl_for_relation_owner(self):
        """Test TTL lookup for owner relation."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Owner relations should return 1 hour
        assert cache._get_ttl_for_relation("owner") == 3600
        assert cache._get_ttl_for_relation("direct_owner") == 3600
        assert cache._get_ttl_for_relation("admin") == 3600

    def test_get_ttl_for_relation_editor(self):
        """Test TTL lookup for editor relation."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Editor relations should return 10 min
        assert cache._get_ttl_for_relation("editor") == 600
        assert cache._get_ttl_for_relation("write") == 600
        assert cache._get_ttl_for_relation("contributor") == 600

    def test_get_ttl_for_relation_viewer(self):
        """Test TTL lookup for viewer relation."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Viewer relations should return 10 min
        assert cache._get_ttl_for_relation("viewer") == 600
        assert cache._get_ttl_for_relation("read") == 600
        assert cache._get_ttl_for_relation("can_read") == 600

    def test_get_ttl_for_relation_denial(self):
        """Test TTL lookup for denial."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Denial should return 1 min
        assert cache._get_ttl_for_relation("any", is_denial=True) == 60

    def test_get_ttl_for_relation_unknown(self):
        """Test TTL lookup for unknown relation falls back to default."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        # Unknown relations should return default TTL
        assert cache._get_ttl_for_relation("custom_relation") == 300

    def test_set_with_relation_owner(self):
        """Test setting cache entry with owner relation uses tiered TTL."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=300)

        cache.set("agent", "alice", "manage", "file", "/doc.txt", True, relation="owner")

        # Verify entry was set
        result = cache.get("agent", "alice", "manage", "file", "/doc.txt")
        assert result is True

        # Verify metadata uses owner TTL (with jitter)
        key = cache._make_key("agent", "alice", "manage", "file", "/doc.txt", None)
        _, jittered_ttl, _, _ = cache._entry_metadata[key]
        # TTL should be around 3600 (±20% jitter)
        assert 2880 <= jittered_ttl <= 4320  # 3600 ± 20%

    def test_set_with_inherited_flag(self):
        """Test setting cache entry with inherited flag uses inherited TTL."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=600)

        cache.set(
            "agent", "alice", "read", "file", "/doc.txt", True, relation="owner", is_inherited=True
        )

        # Verify entry was set
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is True

        # Verify metadata uses inherited TTL (not owner TTL)
        key = cache._make_key("agent", "alice", "read", "file", "/doc.txt", None)
        _, jittered_ttl, _, _ = cache._entry_metadata[key]
        # TTL should be around 300 (inherited), not 3600 (owner)
        assert 240 <= jittered_ttl <= 360  # 300 ± 20%


class TestIssue1077TargetedInvalidation:
    """Test suite for Issue #1077: Targeted invalidation using secondary indexes."""

    def test_invalidation_mode_targeted_default(self):
        """Test that targeted invalidation mode is the default."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)
        assert cache._invalidation_mode == "targeted"

    def test_invalidation_mode_zone_wide(self):
        """Test legacy zone_wide invalidation mode."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, invalidation_mode="zone_wide")
        assert cache._invalidation_mode == "zone_wide"

    def test_indexes_created_on_set(self):
        """Test that secondary indexes are created when setting cache entries."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        cache.set("agent", "alice", "read", "file", "/workspace/doc.txt", True)

        # Subject index should be populated
        subject_key = ("default", "agent", "alice")
        assert subject_key in cache._subject_index
        assert len(cache._subject_index[subject_key]) == 1

        # Object index should be populated
        object_key = ("default", "file", "/workspace/doc.txt")
        assert object_key in cache._object_index
        assert len(cache._object_index[object_key]) == 1

        # Path prefix index should include ancestor paths
        prefix_key = ("default", "file", "/workspace")
        assert prefix_key in cache._path_prefix_index
        assert len(cache._path_prefix_index[prefix_key]) >= 1

    def test_indexes_not_created_in_zone_wide_mode(self):
        """Test that indexes are not created in zone_wide mode."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, invalidation_mode="zone_wide")

        cache.set("agent", "alice", "read", "file", "/workspace/doc.txt", True)

        # Indexes should be empty
        assert len(cache._subject_index) == 0
        assert len(cache._object_index) == 0
        assert len(cache._path_prefix_index) == 0

    def test_targeted_invalidate_subject(self):
        """Test targeted invalidation by subject."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries for alice and bob
        cache.set("agent", "alice", "read", "file", "/doc1.txt", True)
        cache.set("agent", "alice", "write", "file", "/doc2.txt", True)
        cache.set("agent", "bob", "read", "file", "/doc3.txt", True)

        # Verify indexes
        assert len(cache._subject_index[("default", "agent", "alice")]) == 2
        assert len(cache._subject_index[("default", "agent", "bob")]) == 1

        # Invalidate alice
        count = cache.invalidate_subject("agent", "alice")
        assert count == 2

        # Verify alice's entries are gone
        assert cache.get("agent", "alice", "read", "file", "/doc1.txt") is None
        assert cache.get("agent", "alice", "write", "file", "/doc2.txt") is None

        # Bob's entry should still exist
        assert cache.get("agent", "bob", "read", "file", "/doc3.txt") is True

        # Verify indexes are cleaned up
        assert ("default", "agent", "alice") not in cache._subject_index

    def test_targeted_invalidate_object_prefix(self):
        """Test targeted invalidation by path prefix."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries under different paths
        cache.set("agent", "alice", "read", "file", "/workspace/project/a.txt", True)
        cache.set("agent", "bob", "write", "file", "/workspace/project/b.txt", True)
        cache.set("agent", "alice", "read", "file", "/workspace/other/c.txt", True)
        cache.set("agent", "alice", "read", "file", "/home/d.txt", True)

        # Invalidate /workspace/project prefix
        count = cache.invalidate_object_prefix("file", "/workspace/project")
        assert count == 2

        # Entries under /workspace/project should be gone
        assert cache.get("agent", "alice", "read", "file", "/workspace/project/a.txt") is None
        assert cache.get("agent", "bob", "write", "file", "/workspace/project/b.txt") is None

        # Entries under /workspace/other and /home should still exist
        assert cache.get("agent", "alice", "read", "file", "/workspace/other/c.txt") is True
        assert cache.get("agent", "alice", "read", "file", "/home/d.txt") is True

    def test_targeted_invalidate_subject_object_pair(self):
        """Test targeted invalidation for subject-object pair."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.set("agent", "alice", "write", "file", "/doc.txt", True)
        cache.set("agent", "alice", "read", "file", "/other.txt", True)
        cache.set("agent", "bob", "read", "file", "/doc.txt", True)

        # Invalidate only alice's entries for /doc.txt
        count = cache.invalidate_subject_object_pair("agent", "alice", "file", "/doc.txt")
        assert count == 2

        # Only alice's /doc.txt entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is None
        assert cache.get("agent", "alice", "write", "file", "/doc.txt") is None

        # Other entries should remain
        assert cache.get("agent", "alice", "read", "file", "/other.txt") is True
        assert cache.get("agent", "bob", "read", "file", "/doc.txt") is True

    def test_stats_include_targeted_invalidation_metrics(self):
        """Test that stats include targeted invalidation metrics."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.invalidate_subject("agent", "alice")

        stats = cache.get_stats()
        assert "invalidation_mode" in stats
        assert stats["invalidation_mode"] == "targeted"
        assert "targeted_invalidations" in stats
        assert stats["targeted_invalidations"] >= 1
        assert "index_lookups" in stats
        assert "subject_index_size" in stats
        assert "object_index_size" in stats
        assert "path_prefix_index_size" in stats

    def test_clear_also_clears_indexes(self):
        """Test that clear() also clears secondary indexes."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        assert len(cache._subject_index) > 0
        assert len(cache._object_index) > 0

        cache.clear()

        assert len(cache._subject_index) == 0
        assert len(cache._object_index) == 0
        assert len(cache._path_prefix_index) == 0

    def test_path_prefix_index_deep_hierarchy(self):
        """Test path prefix indexing works for deep hierarchies."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entry with deep path
        deep_path = "/workspace/project/src/utils/helpers/common.py"
        cache.set("agent", "alice", "read", "file", deep_path, True)

        # All ancestor prefixes should be indexed
        assert ("default", "file", "/workspace") in cache._path_prefix_index
        assert ("default", "file", "/workspace/project") in cache._path_prefix_index
        assert ("default", "file", "/workspace/project/src") in cache._path_prefix_index
        assert ("default", "file", "/workspace/project/src/utils") in cache._path_prefix_index
        assert (
            "default",
            "file",
            "/workspace/project/src/utils/helpers",
        ) in cache._path_prefix_index

        # Invalidating top-level should remove the entry
        count = cache.invalidate_object_prefix("file", "/workspace")
        assert count == 1
        assert cache.get("agent", "alice", "read", "file", deep_path) is None
