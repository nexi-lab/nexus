"""Tests for cache warmup functionality (Issue #1076)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.cache.warmer import (
    BackgroundCacheWarmer,
    BackgroundWarmupConfig,
    CacheWarmer,
    FileAccessEntry,
    FileAccessTracker,
    WarmupConfig,
    WarmupStats,
    get_file_access_tracker,
    set_file_access_tracker,
)


class TestWarmupStats:
    """Tests for WarmupStats dataclass."""

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        stats = WarmupStats(
            files_warmed=100,
            metadata_warmed=100,
            content_warmed=50,
            permissions_warmed=20,
            bytes_warmed=1024 * 1024 * 10,  # 10MB
            errors=2,
            duration_seconds=5.5,
            skipped=5,
        )

        result = stats.to_dict()

        assert result["files_warmed"] == 100
        assert result["metadata_warmed"] == 100
        assert result["content_warmed"] == 50
        assert result["permissions_warmed"] == 20
        assert result["bytes_warmed"] == 1024 * 1024 * 10
        assert result["bytes_warmed_mb"] == 10.0
        assert result["errors"] == 2
        assert result["duration_seconds"] == 5.5
        assert result["skipped"] == 5


class TestFileAccessTracker:
    """Tests for FileAccessTracker."""

    def test_record_access(self) -> None:
        """Test recording file access."""
        tracker = FileAccessTracker(window_seconds=60, hot_threshold=3)

        tracker.record_access("/test/file.txt", zone_id="zone1", user_id="alice")
        tracker.record_access("/test/file.txt", zone_id="zone1", user_id="bob")

        stats = tracker.get_stats()
        assert stats["tracked_paths"] == 1
        assert stats["total_accesses"] == 2

    def test_get_hot_files(self) -> None:
        """Test getting hot files above threshold."""
        tracker = FileAccessTracker(window_seconds=60, hot_threshold=3)

        # Record enough accesses to make file "hot"
        for _ in range(5):
            tracker.record_access("/hot/file.txt", zone_id="zone1")

        # Record fewer accesses for another file
        tracker.record_access("/cold/file.txt", zone_id="zone1")

        hot_files = tracker.get_hot_files(zone_id="zone1")

        assert len(hot_files) == 1
        assert hot_files[0].path == "/hot/file.txt"
        assert hot_files[0].access_count >= 3

    def test_get_hot_files_filter_by_user(self) -> None:
        """Test filtering hot files by user."""
        tracker = FileAccessTracker(window_seconds=60, hot_threshold=2)

        # Alice accesses file many times
        for _ in range(5):
            tracker.record_access("/shared/file.txt", zone_id="zone1", user_id="alice")

        # Bob accesses different file
        for _ in range(5):
            tracker.record_access("/bob/file.txt", zone_id="zone1", user_id="bob")

        # Filter by alice
        alice_hot = tracker.get_hot_files(zone_id="zone1", user_id="alice")
        assert len(alice_hot) == 1
        assert alice_hot[0].path == "/shared/file.txt"

    def test_get_user_recent_files(self) -> None:
        """Test getting user's recent files."""
        tracker = FileAccessTracker(window_seconds=300, hot_threshold=1)

        # Record accesses for user
        tracker.record_access("/alice/file1.txt", zone_id="zone1", user_id="alice")
        tracker.record_access("/alice/file2.txt", zone_id="zone1", user_id="alice")
        tracker.record_access("/bob/file.txt", zone_id="zone1", user_id="bob")

        recent = tracker.get_user_recent_files(user_id="alice", zone_id="zone1", hours=24)

        assert len(recent) == 2
        assert all(f.path.startswith("/alice/") for f in recent)

    def test_cleanup_stale_entries(self) -> None:
        """Test cleanup of old entries."""
        tracker = FileAccessTracker(window_seconds=1, hot_threshold=1)

        tracker.record_access("/old/file.txt", zone_id="zone1")
        assert tracker.get_stats()["tracked_paths"] == 1

        # Wait for entries to become stale
        time.sleep(2.5)

        removed = tracker.cleanup_stale_entries()
        assert removed == 1
        assert tracker.get_stats()["tracked_paths"] == 0

    def test_max_tracked_paths_eviction(self) -> None:
        """Test LRU eviction when max paths exceeded."""
        tracker = FileAccessTracker(window_seconds=60, hot_threshold=1, max_tracked_paths=5)

        # Add more paths than max
        for i in range(10):
            tracker.record_access(f"/file{i}.txt", zone_id="zone1")

        # Should only have max_tracked_paths
        assert tracker.get_stats()["tracked_paths"] <= 5

    def test_clear(self) -> None:
        """Test clearing all tracking data."""
        tracker = FileAccessTracker()

        tracker.record_access("/test/file.txt", zone_id="zone1")
        assert tracker.get_stats()["tracked_paths"] == 1

        tracker.clear()

        stats = tracker.get_stats()
        assert stats["tracked_paths"] == 0
        assert stats["total_accesses"] == 0


class TestCacheWarmer:
    """Tests for CacheWarmer."""

    @pytest.fixture
    def mock_nexus_fs(self) -> MagicMock:
        """Create mock NexusFS instance."""
        nexus_fs = MagicMock()
        nexus_fs.glob.return_value = ["/test/file1.txt", "/test/file2.txt"]
        nexus_fs.exists.return_value = True
        nexus_fs.read.return_value = b"test content"
        nexus_fs.list.return_value = ["/workspace", "/data"]

        # Mock metadata store
        metadata = MagicMock()
        metadata.get.return_value = MagicMock(size=1000, physical_path="cas/abc123")
        nexus_fs.metadata = metadata

        return nexus_fs

    @pytest.fixture
    def warmer(self, mock_nexus_fs: MagicMock) -> CacheWarmer:
        """Create CacheWarmer instance."""
        config = WarmupConfig(max_files=100, depth=2)
        return CacheWarmer(nexus_fs=mock_nexus_fs, config=config)

    @pytest.mark.asyncio
    async def test_warmup_directory(self, warmer: CacheWarmer) -> None:
        """Test directory warmup."""
        stats = await warmer.warmup_directory(
            path="/test", depth=2, include_content=False, zone_id="zone1"
        )

        assert stats.files_warmed == 2
        assert stats.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_warmup_directory_with_content(
        self, warmer: CacheWarmer, mock_nexus_fs: MagicMock
    ) -> None:
        """Test directory warmup with content."""
        stats = await warmer.warmup_directory(
            path="/test", depth=2, include_content=True, zone_id="zone1"
        )

        assert stats.files_warmed == 2
        # Content should be warmed for small files
        mock_nexus_fs.read.assert_called()

    @pytest.mark.asyncio
    async def test_warmup_from_history(self, mock_nexus_fs: MagicMock) -> None:
        """Test history-based warmup."""
        tracker = FileAccessTracker(window_seconds=300, hot_threshold=2)

        # Record some accesses
        for _ in range(5):
            tracker.record_access("/recent/file.txt", zone_id="zone1", user_id="alice")

        config = WarmupConfig(max_files=100)
        warmer = CacheWarmer(nexus_fs=mock_nexus_fs, config=config, file_tracker=tracker)

        stats = await warmer.warmup_from_history(user="alice", hours=24, zone_id="zone1")

        assert stats.files_warmed >= 0
        assert stats.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_warmup_disabled(self, mock_nexus_fs: MagicMock) -> None:
        """Test that warmup does nothing when disabled."""
        config = WarmupConfig(enabled=False)
        warmer = CacheWarmer(nexus_fs=mock_nexus_fs, config=config)

        stats = await warmer.warmup_directory(path="/test")

        assert stats.files_warmed == 0
        mock_nexus_fs.glob.assert_not_called()

    @pytest.mark.asyncio
    async def test_warmup_paths(self, warmer: CacheWarmer) -> None:
        """Test warming specific paths."""
        paths = ["/specific/file1.txt", "/specific/file2.txt"]

        stats = await warmer.warmup_paths(paths=paths, include_content=False, zone_id="zone1")

        assert stats.files_warmed == 2

    def test_get_stats(self, warmer: CacheWarmer) -> None:
        """Test getting warmer statistics."""
        stats = warmer.get_stats()

        assert "is_warming" in stats
        assert "config" in stats
        assert "current" in stats
        assert stats["config"]["enabled"] is True


class TestBackgroundCacheWarmer:
    """Tests for BackgroundCacheWarmer."""

    @pytest.fixture
    def mock_cache_warmer(self) -> MagicMock:
        """Create mock CacheWarmer."""
        warmer = MagicMock()
        warmer.warmup_paths = AsyncMock(return_value=WarmupStats())
        return warmer

    @pytest.fixture
    def file_tracker(self) -> FileAccessTracker:
        """Create FileAccessTracker with some data."""
        tracker = FileAccessTracker(window_seconds=300, hot_threshold=2)
        for _ in range(5):
            tracker.record_access("/hot/file.txt", zone_id="zone1")
        return tracker

    @pytest.mark.asyncio
    async def test_warmup_cycle(
        self, mock_cache_warmer: MagicMock, file_tracker: FileAccessTracker
    ) -> None:
        """Test single warmup cycle."""
        config = BackgroundWarmupConfig(enabled=True, interval_seconds=1, max_warmup_per_cycle=10)

        bg_warmer = BackgroundCacheWarmer(
            cache_warmer=mock_cache_warmer,
            file_tracker=file_tracker,
            config=config,
        )

        # Run single cycle
        await bg_warmer._warmup_cycle()

        # Should have called warmup_paths with hot files
        mock_cache_warmer.warmup_paths.assert_called_once()

    def test_get_stats(self, mock_cache_warmer: MagicMock, file_tracker: FileAccessTracker) -> None:
        """Test getting background warmer statistics."""
        config = BackgroundWarmupConfig(enabled=True)
        bg_warmer = BackgroundCacheWarmer(
            cache_warmer=mock_cache_warmer,
            file_tracker=file_tracker,
            config=config,
        )

        stats = bg_warmer.get_stats()

        assert "running" in stats
        assert "cycles_completed" in stats
        assert "config" in stats
        assert "tracker_stats" in stats


class TestGlobalTracker:
    """Tests for global FileAccessTracker management."""

    def test_get_creates_singleton(self) -> None:
        """Test that get_file_access_tracker creates a singleton."""
        # Reset global state
        set_file_access_tracker(None)

        tracker1 = get_file_access_tracker()
        tracker2 = get_file_access_tracker()

        assert tracker1 is tracker2

    def test_set_tracker(self) -> None:
        """Test setting custom tracker."""
        custom_tracker = FileAccessTracker(window_seconds=60, hot_threshold=5)
        set_file_access_tracker(custom_tracker)

        tracker = get_file_access_tracker()
        assert tracker is custom_tracker

        # Clean up
        set_file_access_tracker(None)


class TestFileAccessEntry:
    """Tests for FileAccessEntry."""

    def test_cache_key(self) -> None:
        """Test cache key generation."""
        entry = FileAccessEntry(
            path="/test/file.txt",
            zone_id="zone1",
            user_id="alice",
            access_count=5,
            last_access=time.time(),
        )

        key = entry.cache_key
        assert key == ("zone1", "/test/file.txt")
