"""Condition-based revision notification for consistency tokens (Issue #1180 Phase B).

Provides a thread-safe notification mechanism for zone revisions (zookies).
Writers call notify_revision() after a successful write, and readers call
wait_for_revision() to block until the desired revision is available.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class RevisionNotifier:
    """Thread-safe revision notification using per-zone Condition variables.

    Each zone gets its own Condition so that notifications for zone A
    do not wake waiters in zone B.
    """

    def __init__(self) -> None:
        self._revisions: dict[str, int] = {}
        self._conditions: dict[str, threading.Condition] = {}
        self._lock = threading.Lock()

    def _get_condition(self, zone_id: str) -> threading.Condition:
        """Get or create the Condition for a zone (guarded by _lock)."""
        if zone_id not in self._conditions:
            with self._lock:
                # Double-check after acquiring lock
                if zone_id not in self._conditions:
                    self._conditions[zone_id] = threading.Condition()
        return self._conditions[zone_id]

    def notify_revision(self, zone_id: str, revision: int) -> None:
        """Record a new revision and wake all waiters for the zone.

        Only updates if *revision* is strictly greater than the current
        value (monotonic guarantee).
        """
        cond = self._get_condition(zone_id)
        with cond:
            current = self._revisions.get(zone_id, 0)
            if revision > current:
                self._revisions[zone_id] = revision
            cond.notify_all()

    def get_latest_revision(self, zone_id: str) -> int:
        """Return the latest known revision for *zone_id*, or 0 if unknown."""
        return self._revisions.get(zone_id, 0)

    def wait_for_revision(self, zone_id: str, min_revision: int, timeout_ms: int) -> bool:
        """Block until *zone_id* reaches at least *min_revision*.

        Returns True if the revision was reached, False on timeout.
        """
        cond = self._get_condition(zone_id)
        timeout_s = timeout_ms / 1000.0
        with cond:
            return cond.wait_for(
                lambda: self._revisions.get(zone_id, 0) >= min_revision,
                timeout=timeout_s,
            )


class NullRevisionNotifier:
    """No-op fallback used when RevisionNotifier construction fails."""

    def notify_revision(self, zone_id: str, revision: int) -> None:  # noqa: ARG002
        pass

    def get_latest_revision(self, zone_id: str) -> int:  # noqa: ARG002
        return 0

    def wait_for_revision(
        self,
        zone_id: str,  # noqa: ARG002
        min_revision: int,  # noqa: ARG002
        timeout_ms: int,  # noqa: ARG002
    ) -> bool:
        return False
