"""Tests for hotspot detection and proactive cache prefetching (Issue #921)."""

import time
from unittest.mock import MagicMock

import pytest

from nexus.services.permissions.hotspot_detector import (
    HotspotConfig,
    HotspotDetector,
    HotspotEntry,
    HotspotPrefetcher,
)


class TestHotspotConfig:
    """Tests for HotspotConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = HotspotConfig()

        assert config.enabled is True
        assert config.window_seconds == 300
        assert config.hot_threshold == 50
        assert config.prefetch_before_expiry_seconds == 30
        assert config.max_prefetch_batch == 10
        assert config.prefetch_interval_seconds == 10
        assert config.cleanup_interval_seconds == 60

    def test_custom_config(self):
        """Test custom configuration."""
        config = HotspotConfig(
            enabled=False,
            window_seconds=600,
            hot_threshold=100,
            prefetch_before_expiry_seconds=60,
        )

        assert config.enabled is False
        assert config.window_seconds == 600
        assert config.hot_threshold == 100
        assert config.prefetch_before_expiry_seconds == 60


class TestHotspotEntry:
    """Tests for HotspotEntry dataclass."""

    def test_entry_creation(self):
        """Test creating a HotspotEntry."""
        entry = HotspotEntry(
            subject_type="user",
            subject_id="alice",
            resource_type="file",
            permission="read",
            zone_id="default",
            access_count=100,
            last_access=time.time(),
        )

        assert entry.subject_type == "user"
        assert entry.subject_id == "alice"
        assert entry.access_count == 100

    def test_cache_key_tuple(self):
        """Test cache key tuple generation."""
        entry = HotspotEntry(
            subject_type="user",
            subject_id="alice",
            resource_type="file",
            permission="read",
            zone_id="zone1",
            access_count=50,
            last_access=time.time(),
        )

        key = entry.cache_key_tuple()
        assert key == ("user", "alice", "read", "file", "zone1")


class TestHotspotDetector:
    """Tests for HotspotDetector class."""

    def test_record_access(self):
        """Test recording a single access."""
        detector = HotspotDetector()

        detector.record_access(
            subject_type="user",
            subject_id="alice",
            resource_type="file",
            permission="read",
            zone_id="default",
        )

        count = detector.get_access_count(
            subject_type="user",
            subject_id="alice",
            resource_type="file",
            permission="read",
            zone_id="default",
        )
        assert count == 1

    def test_record_access_disabled(self):
        """Test that recording is skipped when disabled."""
        config = HotspotConfig(enabled=False)
        detector = HotspotDetector(config=config)

        detector.record_access(
            subject_type="user",
            subject_id="alice",
            resource_type="file",
            permission="read",
        )

        count = detector.get_access_count(
            subject_type="user",
            subject_id="alice",
            resource_type="file",
            permission="read",
        )
        assert count == 0

    def test_record_access_batch(self):
        """Test recording multiple accesses in batch."""
        detector = HotspotDetector()

        accesses = [
            ("user", "alice", "file", "read", "default"),
            ("user", "alice", "file", "read", "default"),
            ("user", "bob", "file", "write", "default"),
        ]
        detector.record_access_batch(accesses)

        assert detector.get_access_count("user", "alice", "file", "read", "default") == 2
        assert detector.get_access_count("user", "bob", "file", "write", "default") == 1

    def test_is_hot(self):
        """Test hot detection with threshold."""
        config = HotspotConfig(hot_threshold=5)
        detector = HotspotDetector(config=config)

        # Record 4 accesses (below threshold)
        for _ in range(4):
            detector.record_access("user", "alice", "file", "read")

        assert not detector.is_hot("user", "alice", "file", "read")

        # Record 1 more (at threshold)
        detector.record_access("user", "alice", "file", "read")
        assert detector.is_hot("user", "alice", "file", "read")

    def test_get_hot_entries(self):
        """Test getting hot entries sorted by access count."""
        config = HotspotConfig(hot_threshold=3)
        detector = HotspotDetector(config=config)

        # Create different access patterns
        for _ in range(10):
            detector.record_access("user", "power_user", "file", "read")
        for _ in range(5):
            detector.record_access("user", "regular_user", "file", "read")
        for _ in range(2):  # Below threshold
            detector.record_access("user", "casual_user", "file", "read")

        hot_entries = detector.get_hot_entries()

        # Should only include entries above threshold
        assert len(hot_entries) == 2

        # Should be sorted by access count (hottest first)
        assert hot_entries[0].subject_id == "power_user"
        assert hot_entries[0].access_count == 10
        assert hot_entries[1].subject_id == "regular_user"
        assert hot_entries[1].access_count == 5

    def test_get_hot_entries_with_limit(self):
        """Test limiting hot entries returned."""
        config = HotspotConfig(hot_threshold=3)
        detector = HotspotDetector(config=config)

        # Create multiple hot entries
        for i in range(5):
            for _ in range(10 - i):
                detector.record_access("user", f"user_{i}", "file", "read")

        hot_entries = detector.get_hot_entries(limit=2)
        assert len(hot_entries) == 2

    def test_cleanup_stale_entries(self):
        """Test cleanup of stale entries."""
        config = HotspotConfig(window_seconds=1)  # 1 second window
        detector = HotspotDetector(config=config)

        detector.record_access("user", "alice", "file", "read")

        # Wait for entries to become stale
        time.sleep(2.5)  # Window * 2 + buffer

        removed = detector.cleanup_stale_entries()
        assert removed == 1

        # Entry should be gone
        assert detector.get_access_count("user", "alice", "file", "read") == 0

    def test_get_stats(self):
        """Test getting detector statistics."""
        config = HotspotConfig(hot_threshold=5)
        detector = HotspotDetector(config=config)

        # Record some accesses
        for _ in range(10):
            detector.record_access("user", "alice", "file", "read")

        stats = detector.get_stats()

        assert stats["enabled"] is True
        assert stats["window_seconds"] == 300
        assert stats["hot_threshold"] == 5
        assert stats["tracked_keys"] == 1
        assert stats["total_accesses"] == 10

    def test_reset(self):
        """Test resetting all tracking data."""
        detector = HotspotDetector()

        for _ in range(10):
            detector.record_access("user", "alice", "file", "read")

        detector.reset()

        stats = detector.get_stats()
        assert stats["tracked_keys"] == 0
        assert stats["total_accesses"] == 0

    def test_window_pruning(self):
        """Test that old entries outside window are pruned."""
        config = HotspotConfig(window_seconds=1, hot_threshold=200)
        detector = HotspotDetector(config=config)

        # Record many accesses to trigger pruning
        for _ in range(250):
            detector.record_access("user", "alice", "file", "read")

        time.sleep(1.5)  # Wait for some entries to expire

        # Record more to trigger pruning
        for _ in range(50):
            detector.record_access("user", "alice", "file", "read")

        # Count should only include recent entries
        count = detector.get_access_count("user", "alice", "file", "read")
        assert count <= 50  # Only recent entries should remain

    def test_get_prefetch_candidates(self):
        """Test getting prefetch candidates based on cache age."""
        config = HotspotConfig(
            hot_threshold=3,
            prefetch_before_expiry_seconds=30,
            max_prefetch_batch=5,
        )
        detector = HotspotDetector(config=config)

        # Create hot entry
        for _ in range(10):
            detector.record_access("user", "alice", "file", "read", "default")

        # Mock Tiger Cache with cache age close to expiry
        mock_cache = MagicMock()
        mock_cache.get_cache_age.return_value = 280  # 280s age, 300s TTL, 20s until expiry

        candidates = detector.get_prefetch_candidates(mock_cache, cache_ttl=300)

        assert len(candidates) == 1
        assert candidates[0].subject_id == "alice"

    def test_get_prefetch_candidates_no_expiring(self):
        """Test that fresh cache entries are not prefetch candidates."""
        config = HotspotConfig(hot_threshold=3, prefetch_before_expiry_seconds=30)
        detector = HotspotDetector(config=config)

        # Create hot entry
        for _ in range(10):
            detector.record_access("user", "alice", "file", "read", "default")

        # Mock Tiger Cache with fresh cache age
        mock_cache = MagicMock()
        mock_cache.get_cache_age.return_value = 10  # 10s age, plenty of time

        candidates = detector.get_prefetch_candidates(mock_cache, cache_ttl=300)

        assert len(candidates) == 0

    def test_get_prefetch_candidates_not_in_cache(self):
        """Test handling entries not in cache."""
        config = HotspotConfig(hot_threshold=3)
        detector = HotspotDetector(config=config)

        # Create hot entry
        for _ in range(10):
            detector.record_access("user", "alice", "file", "read", "default")

        # Mock Tiger Cache with no cache entry
        mock_cache = MagicMock()
        mock_cache.get_cache_age.return_value = None

        candidates = detector.get_prefetch_candidates(mock_cache, cache_ttl=300)

        assert len(candidates) == 0


class TestHotspotPrefetcher:
    """Tests for HotspotPrefetcher class."""

    def test_init(self):
        """Test prefetcher initialization."""
        detector = HotspotDetector()
        mock_cache = MagicMock()
        mock_updater = MagicMock()

        prefetcher = HotspotPrefetcher(detector, mock_cache, mock_updater)

        assert prefetcher._detector is detector
        assert prefetcher._tiger_cache is mock_cache
        assert prefetcher._tiger_updater is mock_updater
        assert not prefetcher._running

    def test_stop(self):
        """Test stopping the prefetcher."""
        detector = HotspotDetector()
        mock_cache = MagicMock()
        mock_updater = MagicMock()

        prefetcher = HotspotPrefetcher(detector, mock_cache, mock_updater)
        prefetcher._running = True

        prefetcher.stop()

        assert not prefetcher._running

    def test_get_stats(self):
        """Test getting prefetcher statistics."""
        detector = HotspotDetector()
        mock_cache = MagicMock()
        mock_updater = MagicMock()

        prefetcher = HotspotPrefetcher(detector, mock_cache, mock_updater)

        stats = prefetcher.get_stats()

        assert "running" in stats
        assert "prefetch_count" in stats
        assert "last_cycle_duration_seconds" in stats
        assert "detector_stats" in stats

    @pytest.mark.asyncio
    async def test_prefetch_cycle(self):
        """Test a single prefetch cycle."""
        config = HotspotConfig(hot_threshold=3, prefetch_before_expiry_seconds=30)
        detector = HotspotDetector(config=config)

        # Create hot entry
        for _ in range(10):
            detector.record_access("user", "alice", "file", "read", "default")

        # Mock Tiger Cache with expiring entry
        mock_cache = MagicMock()
        mock_cache._cache_ttl = 300
        mock_cache.get_cache_age.return_value = 280  # About to expire

        mock_updater = MagicMock()

        prefetcher = HotspotPrefetcher(detector, mock_cache, mock_updater, config)

        # Run prefetch cycle
        prefetched = await prefetcher._prefetch_cycle()

        assert prefetched == 1
        mock_updater.queue_update.assert_called_once()

        # Verify call arguments
        call_kwargs = mock_updater.queue_update.call_args.kwargs
        assert call_kwargs["subject_type"] == "user"
        assert call_kwargs["subject_id"] == "alice"
        assert call_kwargs["permission"] == "read"
        assert call_kwargs["priority"] == 1  # High priority


class TestIntegration:
    """Integration tests for hotspot detection."""

    def test_end_to_end_flow(self):
        """Test complete flow from access recording to prefetch candidate detection."""
        config = HotspotConfig(
            hot_threshold=5,
            prefetch_before_expiry_seconds=30,
            window_seconds=300,
        )
        detector = HotspotDetector(config=config)

        # Simulate access pattern
        # Power user: 100 accesses
        for _ in range(100):
            detector.record_access("user", "power_user", "file", "read", "zone1")

        # Regular user: 20 accesses
        for _ in range(20):
            detector.record_access("user", "regular_user", "file", "read", "zone1")

        # Casual user: 3 accesses (below threshold)
        for _ in range(3):
            detector.record_access("user", "casual_user", "file", "read", "zone1")

        # Check hot entries
        hot = detector.get_hot_entries()
        assert len(hot) == 2  # Only power_user and regular_user

        # Check stats
        stats = detector.get_stats()
        assert stats["total_accesses"] == 123
        assert stats["tracked_keys"] == 3
        assert stats["hot_entries_detected"] == 2

        # Mock cache for prefetch check
        mock_cache = MagicMock()

        def cache_age_side_effect(**kwargs):
            if kwargs["subject_id"] == "power_user":
                return 290  # About to expire
            elif kwargs["subject_id"] == "regular_user":
                return 100  # Still fresh
            return None

        mock_cache.get_cache_age.side_effect = cache_age_side_effect

        # Get prefetch candidates
        candidates = detector.get_prefetch_candidates(mock_cache, cache_ttl=300)

        # Only power_user should be a candidate (cache about to expire)
        assert len(candidates) == 1
        assert candidates[0].subject_id == "power_user"
