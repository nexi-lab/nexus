"""Tests for RevisionNotifier and NullRevisionNotifier (Issue #1432)."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from nexus.core.revision_notifier import NullRevisionNotifier, RevisionNotifier


class TestRevisionNotifier:
    """Tests for RevisionNotifier."""

    def test_notify_and_get_latest(self) -> None:
        """Notify a revision and verify get_latest_revision returns it."""
        notifier = RevisionNotifier()
        notifier.notify_revision("zone-a", 5)
        assert notifier.get_latest_revision("zone-a") == 5

    def test_get_latest_unknown_zone_returns_zero(self) -> None:
        """Unknown zone should return 0."""
        notifier = RevisionNotifier()
        assert notifier.get_latest_revision("nonexistent") == 0

    def test_wait_for_revision_already_met(self) -> None:
        """wait_for_revision returns True immediately if revision already reached."""
        notifier = RevisionNotifier()
        notifier.notify_revision("z", 10)
        assert notifier.wait_for_revision("z", 10, timeout_ms=100) is True

    def test_wait_for_revision_timeout(self) -> None:
        """wait_for_revision returns False after timeout if revision not reached."""
        notifier = RevisionNotifier()
        start = time.monotonic()
        result = notifier.wait_for_revision("z", 5, timeout_ms=100)
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed >= 0.09  # roughly 100ms

    def test_wait_for_revision_concurrent_notify(self) -> None:
        """Notify from another thread wakes a waiter."""
        notifier = RevisionNotifier()
        results: list[bool] = []

        def waiter() -> None:
            results.append(notifier.wait_for_revision("z", 3, timeout_ms=2000))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)  # let the waiter start
        notifier.notify_revision("z", 3)
        t.join(timeout=3)
        assert results == [True]

    def test_multiple_zones_isolated(self) -> None:
        """Revisions in zone A do not affect zone B."""
        notifier = RevisionNotifier()
        notifier.notify_revision("a", 100)
        assert notifier.get_latest_revision("b") == 0

    def test_monotonic_revisions(self) -> None:
        """An older revision should not overwrite a newer one."""
        notifier = RevisionNotifier()
        notifier.notify_revision("z", 10)
        notifier.notify_revision("z", 5)
        assert notifier.get_latest_revision("z") == 10


class TestNullRevisionNotifier:
    """Tests for NullRevisionNotifier (no-op fallback)."""

    def test_null_notifier_no_ops(self) -> None:
        """All NullRevisionNotifier methods are safe no-ops."""
        null = NullRevisionNotifier()
        null.notify_revision("z", 1)  # should not raise
        assert null.get_latest_revision("z") == 0
        assert null.wait_for_revision("z", 1, timeout_ms=10) is False


class TestLazyInit:
    """Tests for the lazy init pattern in NexusFSCoreMixin."""

    def test_lazy_init_with_classvar(self) -> None:
        """_get_revision_notifier() creates a RevisionNotifier on first call."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        # Reset the class-level notifier
        NexusFSCoreMixin._revision_notifier = None
        mixin = NexusFSCoreMixin()
        notifier = mixin._get_revision_notifier()
        assert isinstance(notifier, RevisionNotifier)
        # Cleanup
        NexusFSCoreMixin._revision_notifier = None

    def test_fallback_on_construction_error(self) -> None:
        """If RevisionNotifier fails to construct, NullRevisionNotifier is used."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        NexusFSCoreMixin._revision_notifier = None

        with patch(
            "nexus.core.revision_notifier.RevisionNotifier.__init__",
            side_effect=RuntimeError("boom"),
        ):
            mixin = NexusFSCoreMixin()
            notifier = mixin._get_revision_notifier()
            assert isinstance(notifier, NullRevisionNotifier)

        # Cleanup
        NexusFSCoreMixin._revision_notifier = None
