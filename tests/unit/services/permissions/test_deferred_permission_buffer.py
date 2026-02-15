"""Tests for DeferredPermissionBuffer class.

This module tests the deferred permission buffer that optimizes
permission operations via batching and background flushing.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from nexus.services.permissions.deferred_permission_buffer import (
    DeferredPermissionBuffer,
    get_default_buffer,
    set_default_buffer,
)


class TestInitialization:
    """Tests for DeferredPermissionBuffer initialization."""

    def test_init_with_default_values(self) -> None:
        """Test initialization with default parameter values."""
        rebac = MagicMock()
        hierarchy = MagicMock()

        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        assert buffer._rebac_manager is rebac
        assert buffer._hierarchy_manager is hierarchy
        assert buffer._flush_interval == 0.1
        assert buffer._max_batch_size == 1000
        assert buffer._started is False
        assert len(buffer._pending_hierarchy) == 0
        assert len(buffer._pending_grants) == 0

    def test_init_with_custom_parameters(self) -> None:
        """Test initialization with custom flush interval and batch size."""
        rebac = MagicMock()
        hierarchy = MagicMock()

        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
            flush_interval_sec=0.5,
            max_batch_size=500,
        )

        assert buffer._flush_interval == 0.5
        assert buffer._max_batch_size == 500

    def test_init_with_none_managers(self) -> None:
        """Test initialization with None managers."""
        buffer = DeferredPermissionBuffer(
            rebac_manager=None,
            hierarchy_manager=None,
        )

        assert buffer._rebac_manager is None
        assert buffer._hierarchy_manager is None
        assert buffer._started is False

    def test_init_stats_are_zero(self) -> None:
        """Test that initial statistics are zero."""
        buffer = DeferredPermissionBuffer()

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 0
        assert stats["pending_grants"] == 0
        assert stats["total_hierarchy_flushed"] == 0
        assert stats["total_grants_flushed"] == 0
        assert stats["flush_count"] == 0


class TestLifecycle:
    """Tests for buffer lifecycle management (start/stop)."""

    def test_start_creates_daemon_thread(self) -> None:
        """Test that start() creates a daemon background thread."""
        buffer = DeferredPermissionBuffer()

        buffer.start()

        assert buffer._started is True
        assert buffer._flush_thread is not None
        assert buffer._flush_thread.is_alive()
        assert buffer._flush_thread.daemon is True

        buffer.stop()

    def test_start_is_idempotent(self) -> None:
        """Test that calling start() twice doesn't create a second thread."""
        buffer = DeferredPermissionBuffer()

        buffer.start()
        first_thread = buffer._flush_thread

        buffer.start()
        second_thread = buffer._flush_thread

        assert first_thread is second_thread
        assert threading.active_count() >= 1

        buffer.stop()

    def test_stop_flushes_remaining_items(self) -> None:
        """Test that stop() flushes all pending items."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        buffer.start()
        buffer.queue_hierarchy("/test/path", "zone1")
        buffer.queue_owner_grant("user1", "/test/file", "zone1")
        buffer.stop()

        # Verify both managers were called
        hierarchy.ensure_parent_tuples_batch.assert_called_once()
        rebac.rebac_write_batch.assert_called_once()

        # Verify queues are empty
        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 0
        assert stats["pending_grants"] == 0

    def test_stop_is_idempotent(self) -> None:
        """Test that calling stop() multiple times is safe."""
        buffer = DeferredPermissionBuffer()

        buffer.start()
        buffer.stop()
        assert buffer._started is False

        # Second stop should be no-op
        buffer.stop()
        assert buffer._started is False

    def test_stop_without_start_is_noop(self) -> None:
        """Test that stop() without start() is a no-op."""
        buffer = DeferredPermissionBuffer()

        # Should not raise
        buffer.stop()
        assert buffer._started is False


class TestQueueOperations:
    """Tests for queue_hierarchy and queue_owner_grant operations."""

    def test_queue_hierarchy_adds_to_pending(self) -> None:
        """Test that queue_hierarchy adds items to pending queue."""
        buffer = DeferredPermissionBuffer()

        buffer.queue_hierarchy("/test/path", "zone1")

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 1
        assert stats["pending_grants"] == 0

    def test_queue_owner_grant_adds_to_pending(self) -> None:
        """Test that queue_owner_grant adds items to pending queue."""
        buffer = DeferredPermissionBuffer()

        buffer.queue_owner_grant("user1", "/test/file", "zone1")

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 0
        assert stats["pending_grants"] == 1

    def test_multiple_items_queue_correctly(self) -> None:
        """Test that multiple items can be queued."""
        buffer = DeferredPermissionBuffer()

        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone1")
        buffer.queue_owner_grant("user1", "/file1", "zone1")
        buffer.queue_owner_grant("user2", "/file2", "zone2")

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 2
        assert stats["pending_grants"] == 2

    def test_queue_is_fifo_order(self) -> None:
        """Test that queue maintains FIFO order."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        # Queue multiple items in specific order
        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone1")
        buffer.queue_hierarchy("/path3", "zone1")

        buffer.flush()

        # Verify order is preserved
        call_args = hierarchy.ensure_parent_tuples_batch.call_args[0]
        paths = call_args[0]
        assert paths == ["/path1", "/path2", "/path3"]

    def test_stats_reflect_queue_sizes(self) -> None:
        """Test that get_stats() accurately reflects queue sizes."""
        buffer = DeferredPermissionBuffer()

        stats1 = buffer.get_stats()
        assert stats1["pending_hierarchy"] == 0
        assert stats1["pending_grants"] == 0

        buffer.queue_hierarchy("/path1", "zone1")
        stats2 = buffer.get_stats()
        assert stats2["pending_hierarchy"] == 1

        buffer.queue_owner_grant("user1", "/file1", "zone1")
        stats3 = buffer.get_stats()
        assert stats3["pending_hierarchy"] == 1
        assert stats3["pending_grants"] == 1

    def test_queue_owner_grant_formats_correctly(self) -> None:
        """Test that queue_owner_grant formats the grant dict correctly."""
        rebac = MagicMock()
        buffer = DeferredPermissionBuffer(rebac_manager=rebac)

        buffer.queue_owner_grant("user123", "/my/file.txt", "zone42")
        buffer.flush()

        rebac.rebac_write_batch.assert_called_once()
        grants = rebac.rebac_write_batch.call_args[0][0]
        assert len(grants) == 1
        assert grants[0] == {
            "subject": ("user", "user123"),
            "relation": "direct_owner",
            "object": ("file", "/my/file.txt"),
            "zone_id": "zone42",
        }


class TestFlushBehavior:
    """Tests for flush behavior and batch processing."""

    def test_flush_processes_hierarchy_via_batch(self) -> None:
        """Test that flush calls hierarchy_manager.ensure_parent_tuples_batch."""
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(hierarchy_manager=hierarchy)

        buffer.queue_hierarchy("/test/path", "zone1")
        buffer.flush()

        hierarchy.ensure_parent_tuples_batch.assert_called_once_with(
            ["/test/path"],
            zone_id="zone1",
        )

    def test_flush_processes_grants_via_batch(self) -> None:
        """Test that flush calls rebac_manager.rebac_write_batch."""
        rebac = MagicMock()
        buffer = DeferredPermissionBuffer(rebac_manager=rebac)

        buffer.queue_owner_grant("user1", "/file", "zone1")
        buffer.flush()

        rebac.rebac_write_batch.assert_called_once()
        grants = rebac.rebac_write_batch.call_args[0][0]
        assert len(grants) == 1

    def test_flush_groups_hierarchy_by_zone_id(self) -> None:
        """Test that flush groups hierarchy items by zone_id."""
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(hierarchy_manager=hierarchy)

        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone2")
        buffer.queue_hierarchy("/path3", "zone1")
        buffer.flush()

        # Should be called twice (once per zone)
        assert hierarchy.ensure_parent_tuples_batch.call_count == 2

        # Extract calls
        calls = hierarchy.ensure_parent_tuples_batch.call_args_list

        # Check that each zone got the right paths
        zone1_calls = [c for c in calls if c[1]["zone_id"] == "zone1"]
        zone2_calls = [c for c in calls if c[1]["zone_id"] == "zone2"]

        assert len(zone1_calls) == 1
        assert len(zone2_calls) == 1

        zone1_paths = zone1_calls[0][0][0]
        zone2_paths = zone2_calls[0][0][0]

        assert set(zone1_paths) == {"/path1", "/path3"}
        assert set(zone2_paths) == {"/path2"}

    def test_flush_clears_queues_after_processing(self) -> None:
        """Test that flush clears queues after successful processing."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        buffer.queue_hierarchy("/path", "zone1")
        buffer.queue_owner_grant("user1", "/file", "zone1")
        buffer.flush()

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 0
        assert stats["pending_grants"] == 0

    def test_flush_with_no_items_is_noop(self) -> None:
        """Test that flush with empty queues doesn't call managers."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        buffer.flush()

        rebac.rebac_write_batch.assert_not_called()
        hierarchy.ensure_parent_tuples_batch.assert_not_called()

    def test_flush_updates_stats_counters(self) -> None:
        """Test that flush updates total flushed counters."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone1")
        buffer.queue_owner_grant("user1", "/file1", "zone1")
        buffer.flush()

        stats = buffer.get_stats()
        assert stats["total_hierarchy_flushed"] == 2
        assert stats["total_grants_flushed"] == 1
        assert stats["flush_count"] == 1

    def test_flush_with_only_hierarchy_manager_none_skips_hierarchy(self) -> None:
        """Test that flush with hierarchy_manager=None skips hierarchy processing."""
        rebac = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=None,
        )

        buffer.queue_hierarchy("/path", "zone1")
        buffer.queue_owner_grant("user1", "/file", "zone1")
        buffer.flush()

        # Only grants should be processed
        rebac.rebac_write_batch.assert_called_once()
        stats = buffer.get_stats()
        assert stats["total_hierarchy_flushed"] == 0
        assert stats["total_grants_flushed"] == 1

    def test_flush_with_only_rebac_manager_none_skips_grants(self) -> None:
        """Test that flush with rebac_manager=None skips grant processing."""
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=None,
            hierarchy_manager=hierarchy,
        )

        buffer.queue_hierarchy("/path", "zone1")
        buffer.queue_owner_grant("user1", "/file", "zone1")
        buffer.flush()

        # Only hierarchy should be processed
        hierarchy.ensure_parent_tuples_batch.assert_called_once()
        stats = buffer.get_stats()
        assert stats["total_hierarchy_flushed"] == 1
        assert stats["total_grants_flushed"] == 0


class TestErrorHandling:
    """Tests for error handling during flush operations."""

    def test_hierarchy_flush_error_requeues_items(self) -> None:
        """Test that hierarchy flush errors re-queue items."""
        hierarchy = MagicMock()
        hierarchy.ensure_parent_tuples_batch.side_effect = Exception("DB error")
        buffer = DeferredPermissionBuffer(hierarchy_manager=hierarchy)

        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone1")
        buffer.flush()

        # Items should be re-queued
        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 2
        assert stats["total_hierarchy_flushed"] == 0

    def test_grant_flush_error_requeues_items(self) -> None:
        """Test that grant flush errors re-queue items."""
        rebac = MagicMock()
        rebac.rebac_write_batch.side_effect = Exception("Write error")
        buffer = DeferredPermissionBuffer(rebac_manager=rebac)

        buffer.queue_owner_grant("user1", "/file1", "zone1")
        buffer.queue_owner_grant("user2", "/file2", "zone1")
        buffer.flush()

        # Items should be re-queued
        stats = buffer.get_stats()
        assert stats["pending_grants"] == 2
        assert stats["total_grants_flushed"] == 0

    def test_flush_loop_catches_exceptions_and_continues(self) -> None:
        """Test that flush loop catches exceptions and continues running."""
        rebac = MagicMock()
        rebac.rebac_write_batch.side_effect = [
            Exception("First error"),
            None,  # Second call succeeds
        ]
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            flush_interval_sec=0.05,
        )

        buffer.start()
        buffer.queue_owner_grant("user1", "/file1", "zone1")

        # Wait for first flush attempt (will fail)
        time.sleep(0.1)

        # Queue another item
        buffer.queue_owner_grant("user2", "/file2", "zone1")

        # Wait for second flush attempt (should succeed)
        time.sleep(0.1)

        buffer.stop()

        # Should have attempted flush at least twice
        assert rebac.rebac_write_batch.call_count >= 2

    def test_error_in_one_batch_type_does_not_block_other(self) -> None:
        """Test that hierarchy error doesn't prevent grant processing."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        hierarchy.ensure_parent_tuples_batch.side_effect = Exception("Hierarchy error")

        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        buffer.queue_hierarchy("/path", "zone1")
        buffer.queue_owner_grant("user1", "/file", "zone1")
        buffer.flush()

        # Hierarchy should be re-queued
        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 1

        # But grants should have succeeded
        rebac.rebac_write_batch.assert_called_once()
        assert stats["total_grants_flushed"] == 1

    def test_grant_error_does_not_block_hierarchy(self) -> None:
        """Test that grant error doesn't prevent hierarchy processing."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        rebac.rebac_write_batch.side_effect = Exception("Grant error")

        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        buffer.queue_hierarchy("/path", "zone1")
        buffer.queue_owner_grant("user1", "/file", "zone1")
        buffer.flush()

        # Grants should be re-queued
        stats = buffer.get_stats()
        assert stats["pending_grants"] == 1

        # But hierarchy should have succeeded
        hierarchy.ensure_parent_tuples_batch.assert_called_once()
        assert stats["total_hierarchy_flushed"] == 1


class TestMaxBatchSize:
    """Tests for max batch size triggering."""

    def test_trigger_flush_called_when_queue_exceeds_max_batch_size(self) -> None:
        """Test that _trigger_flush is called when queue exceeds max_batch_size."""
        buffer = DeferredPermissionBuffer(max_batch_size=3)

        # Mock _trigger_flush to verify it's called
        original_trigger = buffer._trigger_flush
        trigger_called = []

        def mock_trigger():
            trigger_called.append(True)
            original_trigger()

        buffer._trigger_flush = mock_trigger

        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone1")
        assert len(trigger_called) == 0

        buffer.queue_hierarchy("/path3", "zone1")
        assert len(trigger_called) == 1

    def test_both_queue_methods_check_batch_size(self) -> None:
        """Test that both queue methods check and trigger on batch size."""
        buffer = DeferredPermissionBuffer(max_batch_size=2)

        trigger_called = []

        def mock_trigger():
            trigger_called.append(True)

        buffer._trigger_flush = mock_trigger

        # Test queue_hierarchy
        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_owner_grant("user1", "/file1", "zone1")
        assert len(trigger_called) == 1

        # Clear and test queue_owner_grant
        buffer._pending_hierarchy.clear()
        buffer._pending_grants.clear()
        trigger_called.clear()

        buffer.queue_owner_grant("user1", "/file1", "zone1")
        buffer.queue_owner_grant("user2", "/file2", "zone1")
        assert len(trigger_called) == 1

    def test_max_batch_size_counts_total_queue_size(self) -> None:
        """Test that max_batch_size considers total queue size (hierarchy + grants)."""
        buffer = DeferredPermissionBuffer(max_batch_size=3)

        trigger_called = []
        buffer._trigger_flush = lambda: trigger_called.append(True)

        buffer.queue_hierarchy("/path1", "zone1")
        buffer.queue_hierarchy("/path2", "zone1")
        buffer.queue_owner_grant("user1", "/file1", "zone1")

        # Total size is 3, should trigger
        assert len(trigger_called) == 1


class TestThreadSafety:
    """Tests for thread safety of concurrent operations."""

    def test_concurrent_queue_hierarchy_calls(self) -> None:
        """Test that concurrent queue_hierarchy calls are thread-safe."""
        buffer = DeferredPermissionBuffer()

        def queue_items(start_idx: int, count: int):
            for i in range(start_idx, start_idx + count):
                buffer.queue_hierarchy(f"/path{i}", "zone1")

        threads = [
            threading.Thread(target=queue_items, args=(0, 50)),
            threading.Thread(target=queue_items, args=(50, 50)),
            threading.Thread(target=queue_items, args=(100, 50)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 150

    def test_concurrent_queue_owner_grant_calls(self) -> None:
        """Test that concurrent queue_owner_grant calls are thread-safe."""
        buffer = DeferredPermissionBuffer()

        def queue_items(start_idx: int, count: int):
            for i in range(start_idx, start_idx + count):
                buffer.queue_owner_grant(f"user{i}", f"/file{i}", "zone1")

        threads = [
            threading.Thread(target=queue_items, args=(0, 50)),
            threading.Thread(target=queue_items, args=(50, 50)),
            threading.Thread(target=queue_items, args=(100, 50)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = buffer.get_stats()
        assert stats["pending_grants"] == 150

    def test_concurrent_flush_and_queue_operations(self) -> None:
        """Test that concurrent flush and queue operations are thread-safe."""
        rebac = MagicMock()
        hierarchy = MagicMock()
        buffer = DeferredPermissionBuffer(
            rebac_manager=rebac,
            hierarchy_manager=hierarchy,
        )

        stop_flag = threading.Event()

        def queue_continuously():
            idx = 0
            while not stop_flag.is_set():
                buffer.queue_hierarchy(f"/path{idx}", "zone1")
                buffer.queue_owner_grant(f"user{idx}", f"/file{idx}", "zone1")
                idx += 1
                time.sleep(0.001)

        def flush_continuously():
            while not stop_flag.is_set():
                buffer.flush()
                time.sleep(0.01)

        queue_thread = threading.Thread(target=queue_continuously)
        flush_thread = threading.Thread(target=flush_continuously)

        queue_thread.start()
        flush_thread.start()

        # Let them run for a bit
        time.sleep(0.1)

        stop_flag.set()
        queue_thread.join()
        flush_thread.join()

        # Should not crash, and stats should be consistent
        stats = buffer.get_stats()
        assert stats["total_hierarchy_flushed"] >= 0
        assert stats["total_grants_flushed"] >= 0

    def test_get_stats_is_thread_safe(self) -> None:
        """Test that get_stats can be called concurrently with queue operations."""
        buffer = DeferredPermissionBuffer()

        stop_flag = threading.Event()
        stats_results = []

        def queue_items():
            for i in range(100):
                if stop_flag.is_set():
                    break
                buffer.queue_hierarchy(f"/path{i}", "zone1")
                time.sleep(0.001)

        def get_stats_continuously():
            while not stop_flag.is_set():
                stats = buffer.get_stats()
                stats_results.append(stats)
                time.sleep(0.001)

        queue_thread = threading.Thread(target=queue_items)
        stats_thread = threading.Thread(target=get_stats_continuously)

        queue_thread.start()
        stats_thread.start()

        queue_thread.join()
        stop_flag.set()
        stats_thread.join()

        # Should have collected many stats snapshots
        assert len(stats_results) > 0
        # All should be valid dicts
        for stats in stats_results:
            assert "pending_hierarchy" in stats
            assert "pending_grants" in stats


class TestModuleLevelFunctions:
    """Tests for module-level singleton management functions."""

    def test_get_set_default_buffer(self) -> None:
        """Test get/set default buffer functions."""
        # Save original
        original = get_default_buffer()

        try:
            # Should start as None
            set_default_buffer(None)
            assert get_default_buffer() is None

            # Set a buffer
            buffer = DeferredPermissionBuffer()
            set_default_buffer(buffer)
            assert get_default_buffer() is buffer

            # Set to None again
            set_default_buffer(None)
            assert get_default_buffer() is None
        finally:
            # Restore original
            set_default_buffer(original)

    def test_default_buffer_persists_across_calls(self) -> None:
        """Test that default buffer persists across multiple get calls."""
        original = get_default_buffer()

        try:
            buffer = DeferredPermissionBuffer()
            set_default_buffer(buffer)

            # Multiple gets should return same instance
            assert get_default_buffer() is buffer
            assert get_default_buffer() is buffer
            assert get_default_buffer() is buffer
        finally:
            set_default_buffer(original)
