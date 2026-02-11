"""Tests for RevisionNotifier — Condition-based revision notification (Issue #1180 Phase B).

Covers:
1. Immediate return when revision already met
2. Concurrent wakeup from writer thread
3. Timeout returns False
4. Cross-zone isolation (zone A notify does not wake zone B waiters)
5. No-waiters noop (notify without active waiters)
6. Monotonic latest tracking (out-of-order revisions)
7. Multiple concurrent waiters on same zone
8. get_latest_revision returns cached value
"""

from __future__ import annotations

import threading
import time

import pytest

from nexus.core.revision_notifier import RevisionNotifier, RevisionUpdate


# ---------------------------------------------------------------------------
# 1. Immediate return
# ---------------------------------------------------------------------------


class TestImmediateReturn:
    """wait_for_revision returns True immediately when revision already met."""

    def test_immediate_return_when_revision_already_met(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 10)

        start = time.monotonic()
        result = notifier.wait_for_revision("z1", min_revision=5, timeout_ms=5000)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert result is True
        assert elapsed_ms < 50, f"Expected near-instant, took {elapsed_ms:.1f}ms"

    def test_immediate_return_exact_revision(self) -> None:
        """Returns True when latest == min_revision."""
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 7)

        result = notifier.wait_for_revision("z1", min_revision=7, timeout_ms=100)
        assert result is True


# ---------------------------------------------------------------------------
# 2. Concurrent wakeup
# ---------------------------------------------------------------------------


class TestConcurrentWakeup:
    """Writer thread notifies, reader thread wakes up."""

    def test_wakeup_from_writer_thread(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 1)

        result_holder: list[bool] = []

        def waiter() -> None:
            r = notifier.wait_for_revision("z1", min_revision=5, timeout_ms=3000)
            result_holder.append(r)

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        # Give the waiter time to block
        time.sleep(0.02)

        # Writer advances the revision
        notifier.notify_revision("z1", 5)

        t.join(timeout=5)
        assert len(result_holder) == 1
        assert result_holder[0] is True


# ---------------------------------------------------------------------------
# 3. Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    """wait_for_revision returns False when timeout expires."""

    def test_timeout_returns_false(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 1)

        start = time.monotonic()
        result = notifier.wait_for_revision("z1", min_revision=999, timeout_ms=50)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert result is False
        # Should take approximately 50ms (within tolerance)
        assert elapsed_ms >= 40, f"Returned too early: {elapsed_ms:.1f}ms"
        assert elapsed_ms < 200, f"Took too long: {elapsed_ms:.1f}ms"

    def test_timeout_zero_returns_immediately(self) -> None:
        """Zero timeout returns immediately with current state."""
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 1)

        result = notifier.wait_for_revision("z1", min_revision=999, timeout_ms=0)
        assert result is False


# ---------------------------------------------------------------------------
# 4. Cross-zone isolation
# ---------------------------------------------------------------------------


class TestCrossZoneIsolation:
    """Notify on zone A does NOT wake waiters on zone B."""

    def test_cross_zone_isolation(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("zone_a", 1)
        notifier.notify_revision("zone_b", 1)

        zone_b_result: list[bool] = []

        def wait_zone_b() -> None:
            r = notifier.wait_for_revision("zone_b", min_revision=10, timeout_ms=100)
            zone_b_result.append(r)

        t = threading.Thread(target=wait_zone_b, daemon=True)
        t.start()

        # Notify zone_a (should NOT wake zone_b waiter)
        time.sleep(0.02)
        notifier.notify_revision("zone_a", 100)

        t.join(timeout=2)
        assert len(zone_b_result) == 1
        assert zone_b_result[0] is False  # zone_b still at rev 1, timed out


# ---------------------------------------------------------------------------
# 5. No-waiters noop
# ---------------------------------------------------------------------------


class TestNoWaitersNoop:
    """notify_revision with no active waiters does not error."""

    def test_notify_without_waiters(self) -> None:
        notifier = RevisionNotifier()
        # Should not raise or hang
        notifier.notify_revision("z1", 1)
        notifier.notify_revision("z1", 2)
        notifier.notify_revision("z1", 3)

        # Verify the latest was tracked
        assert notifier.get_latest_revision("z1") == 3


# ---------------------------------------------------------------------------
# 6. Monotonic latest tracking
# ---------------------------------------------------------------------------


class TestMonotonicTracking:
    """Out-of-order revisions should not regress the latest."""

    def test_out_of_order_revision_ignored(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 10)
        notifier.notify_revision("z1", 5)  # out-of-order, should be ignored

        assert notifier.get_latest_revision("z1") == 10

    def test_higher_revision_updates_latest(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 5)
        notifier.notify_revision("z1", 10)

        assert notifier.get_latest_revision("z1") == 10


# ---------------------------------------------------------------------------
# 7. Multiple concurrent waiters on same zone
# ---------------------------------------------------------------------------


class TestMultipleConcurrentWaiters:
    """Multiple threads waiting on the same zone all wake up."""

    def test_multiple_waiters_all_wake(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 1)

        results: list[bool] = []
        lock = threading.Lock()

        def waiter() -> None:
            r = notifier.wait_for_revision("z1", min_revision=5, timeout_ms=3000)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=waiter, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()

        time.sleep(0.03)  # Let all waiters block
        notifier.notify_revision("z1", 5)

        for t in threads:
            t.join(timeout=5)

        assert len(results) == 5
        assert all(r is True for r in results)


# ---------------------------------------------------------------------------
# 8. get_latest_revision returns cached value
# ---------------------------------------------------------------------------


class TestGetLatestRevision:
    """get_latest_revision returns the cached latest for each zone."""

    def test_returns_zero_for_unknown_zone(self) -> None:
        notifier = RevisionNotifier()
        assert notifier.get_latest_revision("unknown") == 0

    def test_returns_latest_after_notify(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("z1", 42)
        assert notifier.get_latest_revision("z1") == 42

    def test_independent_per_zone(self) -> None:
        notifier = RevisionNotifier()
        notifier.notify_revision("zone_a", 10)
        notifier.notify_revision("zone_b", 20)

        assert notifier.get_latest_revision("zone_a") == 10
        assert notifier.get_latest_revision("zone_b") == 20


# ---------------------------------------------------------------------------
# RevisionUpdate dataclass
# ---------------------------------------------------------------------------


class TestRevisionUpdate:
    """Tests for the RevisionUpdate frozen dataclass."""

    def test_revision_update_immutable(self) -> None:
        update = RevisionUpdate(zone_id="z1", revision=5, timestamp=1234567890.0)
        assert update.zone_id == "z1"
        assert update.revision == 5
        assert update.timestamp == 1234567890.0

        with pytest.raises(AttributeError):
            update.revision = 10  # type: ignore[misc]
