"""Tests for metadata buffer flush failure and retry behavior.

Verifies that DeferredMetadataBuffer handles transient failures correctly:
items are re-queued on failure, dead-lettered after max retries, and the
background flush loop remains resilient to errors.
"""

import time
from datetime import UTC, datetime

from nexus.contracts.metadata import FileMetadata
from nexus.storage.buffered_metadata_store import DeferredMetadataBuffer
from tests.helpers.dict_metastore import DictMetastore
from tests.helpers.failing_metastore import FailingMetastore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metadata(path: str, version: int = 1) -> FileMetadata:
    now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"/data/{path.strip('/')}",
        size=1024,
        etag="sha256-test",
        created_at=now,
        modified_at=now,
        version=version,
        zone_id="root",
        owner_id="owner-1",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFlushFailureRequeuesItems:
    """test_flush_failure_requeues_items: put_batch fails, items stay in buffer."""

    def test_flush_failure_requeues_items(self) -> None:
        inner = DictMetastore()
        failing_store = FailingMetastore(inner, fail_on=["put_batch"])
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=10.0, max_retries=3)

        m1 = _make_metadata("/a.txt")
        m2 = _make_metadata("/b.txt")
        buf.enqueue(m1)
        buf.enqueue(m2)

        # Flush should fail — items re-queued
        buf.flush()

        stats = buf.get_stats()
        assert stats["pending_metadata"] == 2, "Items should be re-queued after failure"
        assert stats["total_metadata_flushed"] == 0, "Nothing should have been flushed"

        # The items should still be accessible via get_pending
        assert buf.get_pending("/a.txt") is not None
        assert buf.get_pending("/b.txt") is not None

        # The underlying store should not have received anything
        assert inner.get("/a.txt") is None
        assert inner.get("/b.txt") is None


class TestTransientFailureRecovery:
    """test_transient_failure_recovery: fails twice then succeeds."""

    def test_transient_failure_recovery(self) -> None:
        inner = DictMetastore()
        # fail_on_nth=1 means fail on call 1 only; call 2 onwards succeeds
        # We need to fail twice, so we use fail_on_nth=1 with fail_permanently=False
        # and reset after each failure. Instead, use a wrapper approach.
        failing_store = FailingMetastore(inner, fail_on_nth=1, fail_permanently=False)
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=10.0, max_retries=3)

        m1 = _make_metadata("/a.txt")
        buf.enqueue(m1)

        # First flush: call #1 to put_batch -> fails (nth=1)
        buf.flush()
        assert buf.get_stats()["pending_metadata"] == 1, "Item re-queued after first failure"
        assert buf.get_stats()["total_metadata_flushed"] == 0

        # Reset the call counter so the next put_batch also fails (simulates second transient error)
        failing_store.reset()

        # Second flush: call #1 again -> fails
        buf.flush()
        assert buf.get_stats()["pending_metadata"] == 1, "Item re-queued after second failure"
        assert buf.get_stats()["total_metadata_flushed"] == 0

        # Now let it succeed: set fail_on_nth to 0 (never fail by count)
        failing_store._fail_on_nth = 0

        # Third flush: should succeed
        buf.flush()
        assert buf.get_stats()["pending_metadata"] == 0, "Buffer should be empty after success"
        assert buf.get_stats()["total_metadata_flushed"] == 1

        # Verify the item was written to the inner store
        assert inner.get("/a.txt") is not None
        assert inner.get("/a.txt").version == 1


class TestDeadLetterAfterMaxRetries:
    """test_dead_letter_after_max_retries: items move to dead-letter after exhausting retries."""

    def test_dead_letter_after_max_retries(self) -> None:
        inner = DictMetastore()
        failing_store = FailingMetastore(inner, fail_on=["put_batch"])
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=10.0, max_retries=3)

        m1 = _make_metadata("/doomed.txt")
        buf.enqueue(m1)

        # Flush max_retries times — each failure increments retry count
        for _ in range(3):
            buf.flush()

        # After 3 retries, the item should be dead-lettered
        stats = buf.get_stats()
        assert stats["dead_letter_count"] == 1, "Item should be in dead-letter queue"
        assert stats["pending_metadata"] == 0, "Buffer should be empty (item dead-lettered)"

        dead = buf.get_dead_letter()
        assert len(dead) == 1
        assert dead[0]["item"]["path"] == "/doomed.txt"

        # The underlying store should never have received the item
        assert inner.get("/doomed.txt") is None


class TestDeadLetterContainsCorrectInfo:
    """test_dead_letter_contains_correct_info: verify dead-letter entry structure."""

    def test_dead_letter_contains_correct_info(self) -> None:
        inner = DictMetastore()
        failing_store = FailingMetastore(inner, fail_on=["put_batch"])
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=10.0, max_retries=2)

        m1 = _make_metadata("/fail-info.txt", version=42)
        buf.enqueue(m1)

        # Exhaust retries
        for _ in range(2):
            buf.flush()

        dead = buf.get_dead_letter()
        assert len(dead) == 1

        entry = dead[0]
        assert entry["type"] == "metadata"
        assert entry["item"]["path"] == "/fail-info.txt"
        assert entry["item"]["version"] == 42
        assert isinstance(entry["error"], str)
        assert len(entry["error"]) > 0, "Error message should be non-empty"
        assert entry["retries"] == 2


class TestPartialItemsAfterFailure:
    """test_partial_items_after_failure: items remain visible via get_pending() after failure."""

    def test_partial_items_after_failure(self) -> None:
        inner = DictMetastore()
        failing_store = FailingMetastore(inner, fail_on=["put_batch"])
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=10.0, max_retries=5)

        m1 = _make_metadata("/file1.txt", version=1)
        m2 = _make_metadata("/file2.txt", version=2)
        m3 = _make_metadata("/file3.txt", version=3)
        buf.enqueue(m1)
        buf.enqueue(m2)
        buf.enqueue(m3)

        # Flush fails — items should be re-queued
        buf.flush()

        # All items should still be visible via get_pending
        pending1 = buf.get_pending("/file1.txt")
        pending2 = buf.get_pending("/file2.txt")
        pending3 = buf.get_pending("/file3.txt")

        assert pending1 is not None, "file1.txt should still be pending"
        assert pending2 is not None, "file2.txt should still be pending"
        assert pending3 is not None, "file3.txt should still be pending"

        assert pending1.version == 1
        assert pending2.version == 2
        assert pending3.version == 3

        # Buffer should still report 3 pending items
        assert buf.get_stats()["pending_metadata"] == 3


class TestStatsTrackFailures:
    """test_stats_track_failures: stats reflect flushed and dead-letter counts."""

    def test_stats_track_failures(self) -> None:
        inner = DictMetastore()
        # Use fail_on_nth to control which calls fail
        failing_store = FailingMetastore(inner, fail_on_nth=1, fail_permanently=False)
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=10.0, max_retries=2)

        # Enqueue first item
        m1 = _make_metadata("/good.txt", version=1)
        buf.enqueue(m1)

        # First flush fails (call #1)
        buf.flush()
        stats = buf.get_stats()
        assert stats["total_metadata_flushed"] == 0
        assert stats["dead_letter_count"] == 0
        assert stats["pending_metadata"] == 1

        # Second flush also fails — need to reset counter to trigger fail on call #1 again
        # But we want this second flush to also fail so the item is dead-lettered (max_retries=2)
        failing_store.reset()
        buf.flush()
        stats = buf.get_stats()
        assert stats["dead_letter_count"] == 1, "Item should be dead-lettered after 2 failures"
        assert stats["pending_metadata"] == 0
        assert stats["total_metadata_flushed"] == 0

        # Now enqueue a new item and let it succeed
        failing_store._fail_on_nth = 0  # Disable failure injection
        m2 = _make_metadata("/success.txt", version=2)
        buf.enqueue(m2)
        buf.flush()

        stats = buf.get_stats()
        assert stats["total_metadata_flushed"] == 1, "One item should have been flushed"
        assert stats["dead_letter_count"] == 1, "Dead-letter count should remain 1"
        assert stats["pending_metadata"] == 0
        assert stats["flush_count"] == 1, "One successful flush cycle"
        assert stats["total_flushed"] == 1


class TestBackgroundFlushLoopResilience:
    """test_background_flush_loop_resilience: background loop survives transient errors."""

    def test_background_flush_loop_resilience(self) -> None:
        inner = DictMetastore()
        # Fail on first call only, then succeed
        failing_store = FailingMetastore(inner, fail_on_nth=1, fail_permanently=False)
        buf = DeferredMetadataBuffer(failing_store, flush_interval_sec=0.05, max_retries=5)

        # Enqueue before starting — the first background flush will fail
        m1 = _make_metadata("/resilient.txt", version=1)
        buf.enqueue(m1)

        # Start the background flush loop
        buf._start_sync()
        try:
            # Wait long enough for at least 2 flush cycles:
            # - First cycle hits call #1 -> fails, item re-queued
            # - Second cycle hits call #2 -> succeeds
            time.sleep(0.3)

            # The background loop should have recovered and flushed the item
            stats = buf.get_stats()
            assert stats["total_metadata_flushed"] == 1, (
                "Item should have been flushed after transient failure recovery"
            )
            assert stats["pending_metadata"] == 0
            assert stats["dead_letter_count"] == 0

            # Verify the item made it to the underlying store
            assert inner.get("/resilient.txt") is not None

            # Enqueue another item to verify the loop is still running
            m2 = _make_metadata("/after-recovery.txt", version=2)
            buf.enqueue(m2)
            time.sleep(0.2)

            assert inner.get("/after-recovery.txt") is not None, (
                "Background loop should still be functional after error recovery"
            )
        finally:
            buf._stop_sync()
