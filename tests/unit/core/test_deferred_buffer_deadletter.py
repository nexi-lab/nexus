"""Unit tests for DeferredPermissionBuffer error handling and retry paths.

Issue #3192 decision 12A: Test retry logic, error isolation, partial-success
batches, and background flush loop resilience.

The installed version re-queues failed items on (OperationalError,
TimeoutError, RuntimeError) but does NOT track retry counts or have a
dead-letter queue.  These tests document the current behavior and verify
that the retry/re-queue mechanism works correctly, providing a baseline
for the dead-letter enhancements planned in the worktree source.
"""

import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from sqlalchemy.exc import OperationalError

from nexus.bricks.rebac.deferred_permission_buffer import DeferredPermissionBuffer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operational_error(msg: str = "db locked") -> OperationalError:
    """Create a SQLAlchemy OperationalError for testing."""
    return OperationalError(statement="INSERT ...", params={}, orig=Exception(msg))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hierarchy_manager() -> MagicMock:
    mock = MagicMock()
    mock.ensure_parent_tuples_batch = MagicMock()
    return mock


@pytest.fixture
def rebac_manager() -> MagicMock:
    mock = MagicMock()
    mock.rebac_write_batch = MagicMock()
    return mock


@pytest.fixture
def buffer(rebac_manager, hierarchy_manager) -> DeferredPermissionBuffer:
    """Buffer with no background thread -- we drive flushes manually."""
    buf = DeferredPermissionBuffer(
        rebac_manager=rebac_manager,
        hierarchy_manager=hierarchy_manager,
        flush_interval_sec=60,  # long interval so bg thread won't fire
        max_batch_size=1000,
    )
    # Do NOT start the background thread; tests call flush() directly.
    return buf


# ---------------------------------------------------------------------------
# Hierarchy retry tests
# ---------------------------------------------------------------------------


class TestHierarchyRetry:
    def test_hierarchy_flush_retry_on_operational_error(self, buffer, hierarchy_manager):
        """On OperationalError the items must be re-queued for retry."""
        hierarchy_manager.ensure_parent_tuples_batch.side_effect = _make_operational_error()

        buffer.queue_hierarchy("/a/b.txt", "z1")
        buffer.flush()

        # Items should be back in the pending queue
        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] > 0

    def test_hierarchy_repeated_failures_dead_letter_after_max_retries(
        self, buffer, hierarchy_manager
    ):
        """After max_retries failures, items are dead-lettered, not re-queued."""
        hierarchy_manager.ensure_parent_tuples_batch.side_effect = _make_operational_error()

        buffer.queue_hierarchy("/a/b.txt", "z1")

        # Flush max_retries times — item should be dead-lettered
        for _ in range(buffer._max_retries + 1):
            buffer.flush()

        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 0
        assert stats["dead_letter_count"] == 1

    def test_hierarchy_items_not_lost_on_failure(self, buffer, hierarchy_manager):
        """After a failure, all original items must be recoverable."""
        hierarchy_manager.ensure_parent_tuples_batch.side_effect = _make_operational_error()

        buffer.queue_hierarchy("/a.txt", "z1")
        buffer.queue_hierarchy("/b.txt", "z1")
        buffer.flush()

        # Both items should be re-queued
        stats = buffer.get_stats()
        assert stats["pending_hierarchy"] == 2


# ---------------------------------------------------------------------------
# Grants retry tests
# ---------------------------------------------------------------------------


class TestGrantsRetry:
    def test_grants_flush_retry_on_timeout_error(self, buffer, rebac_manager):
        """On TimeoutError the grant items must be re-queued for retry."""
        rebac_manager.rebac_write_batch.side_effect = TimeoutError("timed out")

        buffer.queue_owner_grant("alice", "/doc.txt", "z1")
        buffer.flush()

        stats = buffer.get_stats()
        assert stats["pending_grants"] > 0

    def test_grants_flush_retry_on_runtime_error(self, buffer, rebac_manager):
        """On RuntimeError the grant items must be re-queued for retry."""
        rebac_manager.rebac_write_batch.side_effect = RuntimeError("internal error")

        buffer.queue_owner_grant("alice", "/doc.txt", "z1")
        buffer.flush()

        stats = buffer.get_stats()
        assert stats["pending_grants"] > 0

    def test_grants_repeated_failures_dead_letter_after_max_retries(self, buffer, rebac_manager):
        """After max_retries failures, grants are dead-lettered, not re-queued."""
        rebac_manager.rebac_write_batch.side_effect = TimeoutError("timed out")

        buffer.queue_owner_grant("alice", "/doc.txt", "z1")

        for _ in range(buffer._max_retries + 1):
            buffer.flush()

        stats = buffer.get_stats()
        assert stats["pending_grants"] == 0
        assert stats["dead_letter_count"] == 1


# ---------------------------------------------------------------------------
# Transient error recovery
# ---------------------------------------------------------------------------


class TestTransientErrorRecovery:
    def test_transient_error_retries_success(self, buffer, hierarchy_manager):
        """Fail twice, succeed on third -- items must eventually flush."""
        attempt = {"count": 0}

        def flaky_batch(paths, zone_id):
            attempt["count"] += 1
            if attempt["count"] <= 2:
                raise _make_operational_error("transient")

        hierarchy_manager.ensure_parent_tuples_batch.side_effect = flaky_batch

        buffer.queue_hierarchy("/ok.txt", "z1")

        # First two flushes fail
        buffer.flush()
        assert buffer.get_stats()["pending_hierarchy"] > 0

        buffer.flush()
        assert buffer.get_stats()["pending_hierarchy"] > 0

        # Third flush succeeds
        buffer.flush()
        assert buffer.get_stats()["pending_hierarchy"] == 0
        assert buffer.get_stats()["total_hierarchy_flushed"] > 0

    def test_permanent_error_not_caught(self, buffer, rebac_manager):
        """Errors NOT in the retry-safe tuple (e.g., ValueError) propagate.

        The buffer only catches (OperationalError, TimeoutError, RuntimeError).
        Other exceptions propagate through _flush_sync.
        """
        rebac_manager.rebac_write_batch.side_effect = ValueError("bad data")

        buffer.queue_owner_grant("eve", "/bad.txt", "z1")

        with pytest.raises(ValueError, match="bad data"):
            buffer.flush()


# ---------------------------------------------------------------------------
# Mixed batch / partial success
# ---------------------------------------------------------------------------


class TestMixedBatchPartialSuccess:
    def test_mixed_batch_partial_success(self, buffer, hierarchy_manager, rebac_manager):
        """Hierarchy succeeds, grants fail -- hierarchy flushed, grants re-queued."""
        hierarchy_manager.ensure_parent_tuples_batch.return_value = None
        rebac_manager.rebac_write_batch.side_effect = TimeoutError("grants timeout")

        buffer.queue_hierarchy("/ok.txt", "z1")
        buffer.queue_owner_grant("alice", "/doc.txt", "z1")

        buffer.flush()

        stats = buffer.get_stats()
        assert stats["total_hierarchy_flushed"] > 0
        assert stats["pending_grants"] > 0

    def test_grants_success_hierarchy_fail(self, buffer, hierarchy_manager, rebac_manager):
        """Grants succeed, hierarchy fails -- grants flushed, hierarchy re-queued."""
        hierarchy_manager.ensure_parent_tuples_batch.side_effect = _make_operational_error()
        rebac_manager.rebac_write_batch.return_value = None

        buffer.queue_hierarchy("/fail.txt", "z1")
        buffer.queue_owner_grant("bob", "/ok.txt", "z1")

        buffer.flush()

        stats = buffer.get_stats()
        assert stats["total_grants_flushed"] > 0
        assert stats["pending_hierarchy"] > 0


# ---------------------------------------------------------------------------
# Background flush loop resilience
# ---------------------------------------------------------------------------


class TestFlushLoopResilience:
    @pytest.mark.skipif(
        not hasattr(DeferredPermissionBuffer, "_hierarchy_retry_counts"),
        reason="Requires worktree version of DeferredPermissionBuffer (thread lifecycle test)",
    )
    def test_flush_loop_survives_exception(self, rebac_manager, hierarchy_manager):
        """The background flush loop must not crash on a caught exception."""
        rebac_manager.rebac_write_batch.side_effect = RuntimeError("unexpected")

        buf = DeferredPermissionBuffer(
            rebac_manager=rebac_manager,
            hierarchy_manager=hierarchy_manager,
            flush_interval_sec=0.05,
            max_batch_size=1000,
        )
        buf.start()
        try:
            buf.queue_owner_grant("alice", "/doc.txt", "z1")

            # Give background thread time to attempt flush (> flush_interval)
            time.sleep(0.3)

            # Thread should still be alive despite the error
            assert buf._flush_thread is not None
            assert buf._flush_thread.is_alive()
        finally:
            buf.stop(timeout=2.0)

    @pytest.mark.skipif(
        not hasattr(DeferredPermissionBuffer, "_hierarchy_retry_counts"),
        reason="Requires worktree version of DeferredPermissionBuffer (thread lifecycle test)",
    )
    def test_stop_flushes_remaining_items(self, rebac_manager, hierarchy_manager):
        """stop() must attempt a final flush of remaining items."""
        buf = DeferredPermissionBuffer(
            rebac_manager=rebac_manager,
            hierarchy_manager=hierarchy_manager,
            flush_interval_sec=60,  # long so bg thread won't flush
            max_batch_size=1000,
        )
        buf.start()

        buf.queue_owner_grant("alice", "/doc.txt", "z1")
        buf.queue_hierarchy("/a/b.txt", "z1")

        # stop() calls _flush_sync() as final flush
        buf.stop(timeout=2.0)

        # Verify the managers were called
        rebac_manager.rebac_write_batch.assert_called()
        hierarchy_manager.ensure_parent_tuples_batch.assert_called()

        stats = buf.get_stats()
        assert stats["total_grants_flushed"] > 0
        assert stats["total_hierarchy_flushed"] > 0


# ---------------------------------------------------------------------------
# Stats reporting
# ---------------------------------------------------------------------------


class TestStatsReporting:
    def test_stats_include_all_expected_keys(self, buffer):
        """get_stats() must include pending counts and flush totals."""
        stats = buffer.get_stats()
        expected_keys = {
            "pending_hierarchy",
            "pending_grants",
            "total_hierarchy_flushed",
            "total_grants_flushed",
            "flush_count",
        }
        assert expected_keys.issubset(stats.keys())

    def test_stats_reflect_successful_flush(self, buffer, hierarchy_manager, rebac_manager):
        """Stats must update after a successful flush."""
        buffer.queue_hierarchy("/a.txt", "z1")
        buffer.queue_owner_grant("alice", "/b.txt", "z1")
        buffer.flush()

        stats = buffer.get_stats()
        assert stats["total_hierarchy_flushed"] == 1
        assert stats["total_grants_flushed"] == 1
        assert stats["flush_count"] == 1
        assert stats["pending_hierarchy"] == 0
        assert stats["pending_grants"] == 0

    def test_flush_count_not_incremented_on_failure(self, buffer, hierarchy_manager, rebac_manager):
        """flush_count only increments when items are actually flushed."""
        hierarchy_manager.ensure_parent_tuples_batch.side_effect = _make_operational_error()
        rebac_manager.rebac_write_batch.side_effect = TimeoutError("fail")

        buffer.queue_hierarchy("/a.txt", "z1")
        buffer.queue_owner_grant("alice", "/b.txt", "z1")
        buffer.flush()

        stats = buffer.get_stats()
        # Neither hierarchy nor grants succeeded, so flush_count stays at 0
        assert stats["flush_count"] == 0
