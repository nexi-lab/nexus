"""Tests for adaptive TTL module.

Issue #715: Add Adaptive TTL for Content Cache Based on Write Frequency.
"""

import time

from nexus.core.adaptive_ttl import AdaptiveTTLMixin


class TestAdaptiveTTLMixin:
    """Tests for AdaptiveTTLMixin class."""

    def test_init_default_values(self):
        """Test default initialization values."""
        mixin = AdaptiveTTLMixin()
        assert mixin._base_ttl == 300
        assert mixin._window_seconds == 300.0
        assert mixin._enable_adaptive_ttl is True
        assert mixin._min_ttl == 10
        assert mixin._max_ttl == 600

    def test_init_custom_values(self):
        """Test custom initialization values."""
        mixin = AdaptiveTTLMixin(
            base_ttl=120,
            window_seconds=60.0,
            enable_adaptive_ttl=False,
            min_ttl=5,
            max_ttl=300,
        )
        assert mixin._base_ttl == 120
        assert mixin._window_seconds == 60.0
        assert mixin._enable_adaptive_ttl is False
        assert mixin._min_ttl == 5
        assert mixin._max_ttl == 300

    def test_track_write_single(self):
        """Test tracking a single write."""
        mixin = AdaptiveTTLMixin()
        mixin.track_write("/path/to/file.txt")

        assert "/path/to/file.txt" in mixin._write_frequency
        count, _ = mixin._write_frequency["/path/to/file.txt"]
        assert count == 1

    def test_track_write_multiple(self):
        """Test tracking multiple writes to same key."""
        mixin = AdaptiveTTLMixin()
        for _ in range(5):
            mixin.track_write("/path/to/file.txt")

        count, _ = mixin._write_frequency["/path/to/file.txt"]
        assert count == 5

    def test_track_write_disabled(self):
        """Test that tracking is skipped when disabled."""
        mixin = AdaptiveTTLMixin(enable_adaptive_ttl=False)
        mixin.track_write("/path/to/file.txt")

        assert "/path/to/file.txt" not in mixin._write_frequency

    def test_track_write_bulk(self):
        """Test bulk write tracking."""
        mixin = AdaptiveTTLMixin()
        keys = ["/file1.txt", "/file2.txt", "/file3.txt"]
        mixin.track_write_bulk(keys)

        for key in keys:
            assert key in mixin._write_frequency
            count, _ = mixin._write_frequency[key]
            assert count == 1

    def test_get_adaptive_ttl_no_writes(self):
        """Test TTL for key with no write history."""
        mixin = AdaptiveTTLMixin(base_ttl=300)
        ttl = mixin.get_adaptive_ttl("/unknown/file.txt")
        assert ttl == 300

    def test_get_adaptive_ttl_disabled(self):
        """Test TTL when adaptive TTL is disabled."""
        mixin = AdaptiveTTLMixin(base_ttl=300, enable_adaptive_ttl=False)
        mixin._write_frequency["/file.txt"] = (100, time.time())

        ttl = mixin.get_adaptive_ttl("/file.txt")
        assert ttl == 300  # Returns base TTL

    def test_get_adaptive_ttl_very_high_write_rate(self):
        """Test TTL for very high write rate (>10/min)."""
        mixin = AdaptiveTTLMixin(base_ttl=300, min_ttl=10)

        # Simulate 15 writes in last 30 seconds = 30 writes/min
        current_time = time.time()
        mixin._write_frequency["/hot/file.txt"] = (15, current_time - 30)

        ttl = mixin.get_adaptive_ttl("/hot/file.txt")
        # base_ttl // 6 = 50, but min is 10
        assert ttl == max(10, 300 // 6)  # 50

    def test_get_adaptive_ttl_high_write_rate(self):
        """Test TTL for high write rate (>5/min)."""
        mixin = AdaptiveTTLMixin(base_ttl=300, min_ttl=10)

        # Simulate 8 writes in last 60 seconds = 8 writes/min
        current_time = time.time()
        mixin._write_frequency["/warm/file.txt"] = (8, current_time - 60)

        ttl = mixin.get_adaptive_ttl("/warm/file.txt")
        # base_ttl // 3 = 100
        assert ttl == max(30, 300 // 3)  # 100

    def test_get_adaptive_ttl_moderate_write_rate(self):
        """Test TTL for moderate write rate (>1/min)."""
        mixin = AdaptiveTTLMixin(base_ttl=300, min_ttl=10)

        # Simulate 3 writes in last 60 seconds = 3 writes/min
        current_time = time.time()
        mixin._write_frequency["/moderate/file.txt"] = (3, current_time - 60)

        ttl = mixin.get_adaptive_ttl("/moderate/file.txt")
        # base_ttl // 2 = 150
        assert ttl == max(60, 300 // 2)  # 150

    def test_get_adaptive_ttl_low_write_rate(self):
        """Test TTL for low write rate (<1/min)."""
        mixin = AdaptiveTTLMixin(base_ttl=300, max_ttl=600, window_seconds=600.0)

        # Simulate 1 write in last 2 minutes = 0.5 writes/min (within 600s window)
        current_time = time.time()
        mixin._write_frequency["/cold/file.txt"] = (1, current_time - 120)

        ttl = mixin.get_adaptive_ttl("/cold/file.txt")
        # base_ttl * 2 = 600, capped at max_ttl
        assert ttl == min(600, 300 * 2)  # 600

    def test_get_write_frequency(self):
        """Test getting write frequency for a key."""
        mixin = AdaptiveTTLMixin()

        # No writes
        assert mixin.get_write_frequency("/unknown.txt") == 0.0

        # Simulate 6 writes in last 60 seconds
        current_time = time.time()
        mixin._write_frequency["/file.txt"] = (6, current_time - 60)

        freq = mixin.get_write_frequency("/file.txt")
        assert 5.5 < freq < 6.5  # ~6 writes/min

    def test_clear_write_frequency_single(self):
        """Test clearing write frequency for a single key."""
        mixin = AdaptiveTTLMixin()
        mixin.track_write("/file1.txt")
        mixin.track_write("/file2.txt")

        mixin.clear_write_frequency("/file1.txt")

        assert "/file1.txt" not in mixin._write_frequency
        assert "/file2.txt" in mixin._write_frequency

    def test_clear_write_frequency_all(self):
        """Test clearing all write frequency data."""
        mixin = AdaptiveTTLMixin()
        mixin.track_write("/file1.txt")
        mixin.track_write("/file2.txt")

        mixin.clear_write_frequency()

        assert len(mixin._write_frequency) == 0

    def test_cleanup_stale_entries(self):
        """Test cleanup of stale entries."""
        mixin = AdaptiveTTLMixin(window_seconds=60.0)

        # Add recent entry
        mixin.track_write("/recent.txt")

        # Add stale entry (outside 2x window)
        mixin._write_frequency["/stale.txt"] = (5, time.time() - 150)

        removed = mixin.cleanup_stale_entries()

        assert removed == 1
        assert "/stale.txt" not in mixin._write_frequency
        assert "/recent.txt" in mixin._write_frequency

    def test_get_adaptive_ttl_stats(self):
        """Test getting adaptive TTL statistics."""
        mixin = AdaptiveTTLMixin(base_ttl=300)
        mixin.track_write("/file1.txt")
        mixin.track_write("/file2.txt")

        stats = mixin.get_adaptive_ttl_stats()

        assert stats["enabled"] is True
        assert stats["base_ttl"] == 300
        assert stats["tracked_keys"] == 2
        assert stats["total_writes_tracked"] == 2

    def test_window_reset(self):
        """Test that write counter resets after window expires."""
        mixin = AdaptiveTTLMixin(window_seconds=60.0)

        # Simulate old write outside window
        mixin._write_frequency["/file.txt"] = (10, time.time() - 120)

        # New write should reset counter
        mixin.track_write("/file.txt")

        count, _ = mixin._write_frequency["/file.txt"]
        assert count == 1


class TestAdaptiveTTLIntegration:
    """Integration tests for adaptive TTL with MetadataCache."""

    def test_metadata_cache_with_adaptive_ttl(self):
        """Test MetadataCache uses adaptive TTL correctly."""
        from nexus.storage.cache import MetadataCache

        cache = MetadataCache(
            ttl_seconds=300,
            enable_adaptive_ttl=True,
        )

        # Simulate writes to make path "hot"
        for _ in range(15):
            cache.track_write("/hot/file.txt")

        # TTL should be reduced for hot file
        ttl = cache.get_adaptive_ttl("/hot/file.txt")
        assert ttl < 300  # Less than base TTL

    def test_metadata_cache_invalidate_tracks_write(self):
        """Test that invalidate_path tracks writes for adaptive TTL."""
        from nexus.storage.cache import MetadataCache

        cache = MetadataCache(
            ttl_seconds=300,
            enable_adaptive_ttl=True,
        )

        # Invalidate should track write
        cache.invalidate_path("/file.txt")

        assert "/file.txt" in cache._write_frequency
        count, _ = cache._write_frequency["/file.txt"]
        assert count == 1

    def test_metadata_cache_stats_include_adaptive_ttl(self):
        """Test that get_stats includes adaptive TTL info."""
        from nexus.storage.cache import MetadataCache

        cache = MetadataCache(
            ttl_seconds=300,
            enable_adaptive_ttl=True,
        )

        stats = cache.get_stats()

        assert "adaptive_ttl" in stats
        assert stats["adaptive_ttl"]["enabled"] is True

    def test_metadata_cache_disabled_adaptive_ttl(self):
        """Test MetadataCache with adaptive TTL disabled."""
        from nexus.storage.cache import MetadataCache

        cache = MetadataCache(
            ttl_seconds=300,
            enable_adaptive_ttl=False,
        )

        # Should return base TTL regardless of writes
        for _ in range(20):
            cache.invalidate_path("/file.txt")

        ttl = cache.get_adaptive_ttl("/file.txt")
        assert ttl == 300  # Always base TTL

    def test_metadata_cache_no_ttl_no_adaptive(self):
        """Test MetadataCache without TTL doesn't use adaptive TTL."""
        from nexus.storage.cache import MetadataCache

        cache = MetadataCache(
            ttl_seconds=None,  # No TTL
            enable_adaptive_ttl=True,  # This gets ignored
        )

        stats = cache.get_stats()
        assert stats["adaptive_ttl"]["enabled"] is False
