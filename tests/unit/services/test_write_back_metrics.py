"""Unit tests for WriteBackMetrics (Issue #1129).

Tests thread-safe counters, per-backend breakdown, and snapshot.
"""

from __future__ import annotations

import threading

from nexus.services.write_back_metrics import WriteBackMetrics


class TestWriteBackMetrics:
    """Tests for WriteBackMetrics counters."""

    def test_initial_snapshot_all_zeros(self) -> None:
        metrics = WriteBackMetrics()
        snap = metrics.snapshot()
        assert snap["changes_pushed"] == 0
        assert snap["changes_failed"] == 0
        assert snap["conflicts_detected"] == 0
        assert snap["conflicts_auto_resolved"] == 0
        assert snap["per_backend"] == {}

    def test_record_push_increments(self) -> None:
        metrics = WriteBackMetrics()
        metrics.record_push("gcs")
        metrics.record_push("gcs")
        metrics.record_push("s3")

        snap = metrics.snapshot()
        assert snap["changes_pushed"] == 3
        assert snap["per_backend"]["gcs"]["pushed"] == 2
        assert snap["per_backend"]["s3"]["pushed"] == 1

    def test_record_failure_increments(self) -> None:
        metrics = WriteBackMetrics()
        metrics.record_failure("gcs")
        metrics.record_failure("gcs")

        snap = metrics.snapshot()
        assert snap["changes_failed"] == 2
        assert snap["per_backend"]["gcs"]["failed"] == 2

    def test_record_conflict_auto_resolved(self) -> None:
        metrics = WriteBackMetrics()
        metrics.record_conflict("gcs", auto_resolved=True)

        snap = metrics.snapshot()
        assert snap["conflicts_detected"] == 1
        assert snap["conflicts_auto_resolved"] == 1
        assert snap["per_backend"]["gcs"]["conflicts"] == 1

    def test_record_conflict_not_auto_resolved(self) -> None:
        metrics = WriteBackMetrics()
        metrics.record_conflict("gcs", auto_resolved=False)

        snap = metrics.snapshot()
        assert snap["conflicts_detected"] == 1
        assert snap["conflicts_auto_resolved"] == 0

    def test_mixed_operations(self) -> None:
        metrics = WriteBackMetrics()
        metrics.record_push("gcs")
        metrics.record_failure("gcs")
        metrics.record_conflict("gcs")
        metrics.record_push("s3")

        snap = metrics.snapshot()
        assert snap["changes_pushed"] == 2
        assert snap["changes_failed"] == 1
        assert snap["conflicts_detected"] == 1
        assert len(snap["per_backend"]) == 2
        assert snap["per_backend"]["gcs"]["pushed"] == 1
        assert snap["per_backend"]["gcs"]["failed"] == 1

    def test_reset_clears_all(self) -> None:
        metrics = WriteBackMetrics()
        metrics.record_push("gcs")
        metrics.record_failure("s3")
        metrics.record_conflict("gcs")
        metrics.reset()

        snap = metrics.snapshot()
        assert snap["changes_pushed"] == 0
        assert snap["changes_failed"] == 0
        assert snap["conflicts_detected"] == 0
        assert snap["per_backend"] == {}

    def test_snapshot_returns_copy(self) -> None:
        """Snapshot dict should be independent of internal state."""
        metrics = WriteBackMetrics()
        metrics.record_push("gcs")
        snap1 = metrics.snapshot()

        metrics.record_push("gcs")
        snap2 = metrics.snapshot()

        assert snap1["changes_pushed"] == 1
        assert snap2["changes_pushed"] == 2

    def test_thread_safety(self) -> None:
        """Concurrent updates should not lose counts."""
        metrics = WriteBackMetrics()
        iterations = 1000

        def push_worker() -> None:
            for _ in range(iterations):
                metrics.record_push("backend")

        def fail_worker() -> None:
            for _ in range(iterations):
                metrics.record_failure("backend")

        threads = [
            threading.Thread(target=push_worker),
            threading.Thread(target=fail_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = metrics.snapshot()
        assert snap["changes_pushed"] == iterations
        assert snap["changes_failed"] == iterations
        assert snap["per_backend"]["backend"]["pushed"] == iterations
        assert snap["per_backend"]["backend"]["failed"] == iterations
