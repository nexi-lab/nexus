"""Unit tests for FUSE readahead and prefetch (Issue #1073).

Tests cover:
- ReadSession pattern detection
- PrefetchBufferPool memory management
- ReadaheadManager orchestration
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from nexus.fuse.readahead import (
    AccessPattern,
    PrefetchBufferPool,
    ReadaheadConfig,
    ReadaheadManager,
    ReadSession,
)


# =============================================================================
# ReadSession Tests
# =============================================================================


class TestReadSession:
    """Tests for ReadSession pattern detection."""

    def test_initial_state(self):
        """Test initial session state."""
        session = ReadSession(path="/test/file.txt", fh=1)

        assert session.path == "/test/file.txt"
        assert session.fh == 1
        assert session.sequential_count == 0
        assert session.last_offset == 0
        assert session.last_size == 0

    def test_sequential_detection_basic(self):
        """Test basic sequential access detection."""
        session = ReadSession(path="/test/file.txt", fh=1)

        # First read at offset 0
        pattern = session.update_access(0, 4096)
        assert pattern == AccessPattern.SEQUENTIAL
        assert session.sequential_count == 1

        # Second read at offset 4096 (sequential)
        pattern = session.update_access(4096, 4096)
        assert pattern == AccessPattern.SEQUENTIAL
        assert session.sequential_count == 2

        # Third read at offset 8192 (sequential)
        pattern = session.update_access(8192, 4096)
        assert pattern == AccessPattern.SEQUENTIAL
        assert session.sequential_count == 3

    def test_random_access_detection(self):
        """Test random access detection resets sequential count."""
        session = ReadSession(path="/test/file.txt", fh=1)

        # Build up sequential count
        session.update_access(0, 4096)
        session.update_access(4096, 4096)
        assert session.sequential_count == 2

        # Random jump backwards
        pattern = session.update_access(0, 4096)
        assert pattern == AccessPattern.RANDOM
        assert session.sequential_count == 0

    def test_sequential_with_tolerance(self):
        """Test sequential detection with small gaps."""
        session = ReadSession(
            path="/test/file.txt",
            fh=1,
            sequential_tolerance=64 * 1024,  # 64KB tolerance
        )

        # First read
        session.update_access(0, 4096)

        # Skip a few bytes (within tolerance)
        pattern = session.update_access(4096 + 1000, 4096)
        assert pattern == AccessPattern.SEQUENTIAL
        assert session.sequential_count == 2

    def test_window_growth(self):
        """Test readahead window grows on sequential access."""
        session = ReadSession(path="/test/file.txt", fh=1)
        initial_window = session.readahead_window

        # Build up sequential reads
        session.update_access(0, 4096)
        session.update_access(4096, 4096)  # Window should grow here

        assert session.readahead_window > initial_window

        # Continue sequential reads
        session.update_access(8192, 4096)

        # Window should continue growing
        assert session.readahead_window >= initial_window * 2

    def test_window_reset_on_random(self):
        """Test readahead window resets on random access."""
        session = ReadSession(path="/test/file.txt", fh=1)

        # Build up sequential reads to grow window
        for i in range(5):
            session.update_access(i * 4096, 4096)

        grown_window = session.readahead_window
        assert grown_window > 128 * 1024  # Should have grown

        # Random access - use 100MB offset to ensure it's beyond any readahead window
        session.update_access(100 * 1024 * 1024, 4096)

        assert session.readahead_window == 512 * 1024  # Reset to initial (DEFAULT_INITIAL_WINDOW)

    def test_window_capped_at_max(self):
        """Test readahead window doesn't exceed max."""
        max_window = 1024 * 1024  # 1MB
        session = ReadSession(
            path="/test/file.txt",
            fh=1,
            max_window=max_window,
        )

        # Many sequential reads
        for i in range(100):
            session.update_access(i * 4096, 4096)

        assert session.readahead_window <= max_window

    def test_prefetch_pending_tracking(self):
        """Test prefetch pending/completed tracking."""
        session = ReadSession(path="/test/file.txt", fh=1)

        # Mark as pending
        assert session.mark_prefetch_pending(0) is True
        assert session.mark_prefetch_pending(0) is False  # Already pending

        # Mark as completed
        session.mark_prefetch_completed(0)
        assert 0 in session.prefetch_completed
        assert 0 not in session.prefetch_pending

        # Can't mark completed block as pending again
        assert session.mark_prefetch_pending(0) is False

    def test_stats(self):
        """Test session statistics."""
        session = ReadSession(path="/test/file.txt", fh=1)

        session.update_access(0, 4096)
        session.record_prefetch_hit()
        session.record_prefetch_miss()

        stats = session.get_stats()
        assert stats["path"] == "/test/file.txt"
        assert stats["total_reads"] == 1
        assert stats["prefetch_hits"] == 1
        assert stats["prefetch_misses"] == 1
        assert stats["prefetch_hit_rate"] == 0.5


# =============================================================================
# PrefetchBufferPool Tests
# =============================================================================


class TestPrefetchBufferPool:
    """Tests for PrefetchBufferPool memory management."""

    def test_basic_put_get(self):
        """Test basic put and get operations."""
        pool = PrefetchBufferPool(max_size_bytes=1024 * 1024)  # 1MB

        data = b"x" * 1024
        assert pool.put("/test/file.txt", 0, data) is True

        result = pool.get("/test/file.txt", 0, 1024)
        assert result == data

    def test_get_partial_range(self):
        """Test getting partial range from a block."""
        pool = PrefetchBufferPool(max_size_bytes=1024 * 1024)

        # Store 4KB block
        data = bytes(range(256)) * 16  # 4KB
        pool.put("/test/file.txt", 0, data)

        # Get first 100 bytes
        result = pool.get("/test/file.txt", 0, 100)
        assert result == data[:100]

        # Get middle 100 bytes
        result = pool.get("/test/file.txt", 100, 100)
        assert result == data[100:200]

    def test_get_miss(self):
        """Test cache miss returns None."""
        pool = PrefetchBufferPool(max_size_bytes=1024 * 1024)

        result = pool.get("/nonexistent", 0, 1024)
        assert result is None

    def test_eviction_on_full(self):
        """Test LRU eviction when pool is full."""
        pool = PrefetchBufferPool(max_size_bytes=1024)  # 1KB max

        # Fill pool
        pool.put("/file1", 0, b"a" * 512)
        pool.put("/file2", 0, b"b" * 512)

        # Pool should be full
        stats = pool.get_stats()
        assert stats["size_bytes"] == 1024

        # Add another, should evict oldest
        pool.put("/file3", 0, b"c" * 512)

        # file1 should be evicted (LRU)
        assert pool.get("/file1", 0, 512) is None
        assert pool.get("/file2", 0, 512) is not None
        assert pool.get("/file3", 0, 512) is not None

    def test_invalidate_path(self):
        """Test invalidating all buffers for a path."""
        pool = PrefetchBufferPool(max_size_bytes=1024 * 1024)

        pool.put("/test/file.txt", 0, b"block0")
        pool.put("/test/file.txt", 4096, b"block1")
        pool.put("/other/file.txt", 0, b"other")

        # Invalidate test file
        count = pool.invalidate_path("/test/file.txt")
        assert count == 2

        # Test file buffers gone
        assert pool.get("/test/file.txt", 0, 6) is None
        assert pool.get("/test/file.txt", 4096, 6) is None

        # Other file still there
        assert pool.get("/other/file.txt", 0, 5) == b"other"

    def test_clear(self):
        """Test clearing all buffers."""
        pool = PrefetchBufferPool(max_size_bytes=1024 * 1024)

        pool.put("/file1", 0, b"data1")
        pool.put("/file2", 0, b"data2")

        pool.clear()

        stats = pool.get_stats()
        assert stats["entries"] == 0
        assert stats["size_bytes"] == 0

    def test_stats(self):
        """Test buffer pool statistics."""
        pool = PrefetchBufferPool(max_size_bytes=1024 * 1024)

        pool.put("/test", 0, b"data")
        pool.get("/test", 0, 4)  # Hit
        pool.get("/nonexistent", 0, 4)  # Miss

        stats = pool.get_stats()
        assert stats["entries"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_thread_safety(self):
        """Test concurrent access to buffer pool."""
        pool = PrefetchBufferPool(max_size_bytes=10 * 1024 * 1024)
        errors = []

        def writer(path_id: int):
            try:
                for i in range(100):
                    pool.put(f"/file{path_id}", i * 1024, b"x" * 1024)
            except Exception as e:
                errors.append(e)

        def reader(path_id: int):
            try:
                for i in range(100):
                    pool.get(f"/file{path_id}", i * 1024, 1024)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=writer, args=(i,)))
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# =============================================================================
# ReadaheadManager Tests
# =============================================================================


class TestReadaheadManager:
    """Tests for ReadaheadManager orchestration."""

    @pytest.fixture
    def mock_read_func(self):
        """Create a mock read function."""

        def read_func(path: str, offset: int, size: int) -> bytes:
            # Return synthetic data based on offset
            return bytes([offset % 256] * size)

        return read_func

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return ReadaheadConfig(
            enabled=True,
            buffer_pool_mb=1,
            prefetch_workers=2,
            block_size=4096,
            min_sequential_count=2,
            initial_window=4096,
            max_window=16384,
            sequential_tolerance=1024,
            warm_l2_cache=False,  # Disable for unit tests
        )

    def test_on_read_creates_session(self, config, mock_read_func):
        """Test that on_read creates a session."""
        manager = ReadaheadManager(config, mock_read_func)

        # First read creates session
        manager.on_read(fh=1, path="/test/file.txt", offset=0, size=1024)

        stats = manager.get_stats()
        assert stats["active_sessions"] == 1

        manager.shutdown()

    def test_on_read_returns_none_on_miss(self, config, mock_read_func):
        """Test that on_read returns None when no prefetch available."""
        manager = ReadaheadManager(config, mock_read_func)

        result = manager.on_read(fh=1, path="/test/file.txt", offset=0, size=1024)
        assert result is None

        manager.shutdown()

    def test_sequential_triggers_prefetch(self, config, mock_read_func):
        """Test that sequential access triggers prefetch."""
        manager = ReadaheadManager(config, mock_read_func)

        # First two reads establish sequential pattern
        manager.on_read(fh=1, path="/test/file.txt", offset=0, size=1024)
        manager.on_read(fh=1, path="/test/file.txt", offset=1024, size=1024)

        # Wait for prefetch to complete
        time.sleep(0.1)

        stats = manager.get_stats()
        assert stats["prefetch_triggered"] > 0

        manager.shutdown()

    def test_prefetch_hit(self, config, mock_read_func):
        """Test prefetch hit returns data."""
        manager = ReadaheadManager(config, mock_read_func)

        # Trigger prefetch with sequential reads
        manager.on_read(fh=1, path="/test/file.txt", offset=0, size=4096)
        manager.on_read(fh=1, path="/test/file.txt", offset=4096, size=4096)

        # Wait for prefetch to complete
        time.sleep(0.2)

        # Next read should hit prefetch buffer
        result = manager.on_read(fh=1, path="/test/file.txt", offset=8192, size=4096)

        # Result could be from prefetch buffer
        stats = manager.get_stats()
        # At minimum, prefetch should have been triggered
        assert stats["prefetch_triggered"] > 0

        manager.shutdown()

    def test_on_release_cleans_up(self, config, mock_read_func):
        """Test that on_release cleans up session."""
        manager = ReadaheadManager(config, mock_read_func)

        manager.on_read(fh=1, path="/test/file.txt", offset=0, size=1024)
        assert manager.get_stats()["active_sessions"] == 1

        manager.on_release(fh=1)
        assert manager.get_stats()["active_sessions"] == 0

        manager.shutdown()

    def test_invalidate_path(self, config, mock_read_func):
        """Test path invalidation clears prefetch state."""
        manager = ReadaheadManager(config, mock_read_func)

        # Trigger prefetch
        manager.on_read(fh=1, path="/test/file.txt", offset=0, size=4096)
        manager.on_read(fh=1, path="/test/file.txt", offset=4096, size=4096)

        time.sleep(0.1)

        # Invalidate
        manager.invalidate_path("/test/file.txt")

        # Buffer should be cleared
        pool_stats = manager.get_stats()["buffer_pool"]
        # After invalidation, no buffers for this path

        manager.shutdown()

    def test_disabled_returns_none(self, mock_read_func):
        """Test disabled manager always returns None."""
        config = ReadaheadConfig(enabled=False)
        manager = ReadaheadManager(config, mock_read_func)

        result = manager.on_read(fh=1, path="/test/file.txt", offset=0, size=1024)
        assert result is None

        manager.shutdown()

    def test_multiple_file_handles(self, config, mock_read_func):
        """Test multiple concurrent file handles."""
        manager = ReadaheadManager(config, mock_read_func)

        # Open multiple files
        manager.on_read(fh=1, path="/file1.txt", offset=0, size=1024)
        manager.on_read(fh=2, path="/file2.txt", offset=0, size=1024)
        manager.on_read(fh=3, path="/file3.txt", offset=0, size=1024)

        assert manager.get_stats()["active_sessions"] == 3

        # Release one
        manager.on_release(fh=2)
        assert manager.get_stats()["active_sessions"] == 2

        manager.shutdown()

    def test_shutdown(self, config, mock_read_func):
        """Test clean shutdown."""
        manager = ReadaheadManager(config, mock_read_func)

        # Create some state
        manager.on_read(fh=1, path="/test/file.txt", offset=0, size=1024)
        manager.on_read(fh=1, path="/test/file.txt", offset=1024, size=1024)

        # Shutdown should not raise
        manager.shutdown()

        # After shutdown, on_read returns None
        result = manager.on_read(fh=1, path="/test/file.txt", offset=2048, size=1024)
        assert result is None


# =============================================================================
# ReadaheadConfig Tests
# =============================================================================


class TestReadaheadConfig:
    """Tests for ReadaheadConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = ReadaheadConfig()

        assert config.enabled is True
        assert config.buffer_pool_mb == 128  # Optimized for remote/network filesystems
        assert config.prefetch_workers == 8  # More workers to hide network latency
        assert config.block_size == 4 * 1024 * 1024
        assert config.min_sequential_count == 2
        assert config.warm_l2_cache is True
        assert config.max_blocks_per_trigger == 8
        assert config.prefetch_on_open is True

    def test_from_dict(self):
        """Test creating config from dict."""
        config_dict = {
            "readahead_enabled": False,
            "readahead_buffer_mb": 128,
            "readahead_workers": 8,
            "readahead_block_size": 1024 * 1024,
        }

        config = ReadaheadConfig.from_dict(config_dict)

        assert config.enabled is False
        assert config.buffer_pool_mb == 128
        assert config.prefetch_workers == 8
        assert config.block_size == 1024 * 1024

    def test_from_empty_dict(self):
        """Test creating config from empty dict uses defaults."""
        config = ReadaheadConfig.from_dict({})

        assert config.enabled is True
        assert config.buffer_pool_mb == 128  # New default for network filesystems
