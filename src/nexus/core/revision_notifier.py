"""Condition-based revision notification for NexusFS (Issue #1180 Phase B).

Replaces polling-based `_wait_for_revision()` with `threading.Condition`
notification. Writers call `notify_revision()` after each write; readers
call `wait_for_revision()` to block until the target revision is reached.

Performance:
    - notify_revision: ~1μs (uncontended Condition.notify_all)
    - wait_for_revision (immediate): ~1μs (check + return)
    - wait_for_revision (blocking): wakes within μs of notify

Thread safety:
    All methods are safe for concurrent use from multiple threads.
    Each zone gets its own Condition to avoid cross-zone wakeup storms.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RevisionUpdate:
    """Immutable record of a revision change."""

    zone_id: str
    revision: int
    timestamp: float


class RevisionNotifier:
    """Condition-based notification for zone revision changes.

    Per-zone Conditions ensure that waiters in zone A are not woken by
    writes to zone B. The latest known revision is cached to allow
    immediate return when the target is already met.
    """

    def __init__(self) -> None:
        self._conditions: dict[str, threading.Condition] = {}
        self._latest: dict[str, int] = {}
        self._guard = threading.Lock()

    def _get_condition(self, zone_id: str) -> threading.Condition:
        """Get or create the Condition for a zone."""
        with self._guard:
            if zone_id not in self._conditions:
                self._conditions[zone_id] = threading.Condition()
                self._latest[zone_id] = 0
            return self._conditions[zone_id]

    def notify_revision(self, zone_id: str, revision: int) -> None:
        """Notify all waiters that a new revision is available.

        Called by the write path after a successful revision increment.

        Args:
            zone_id: The zone whose revision changed.
            revision: The new revision number.
        """
        cond = self._get_condition(zone_id)
        with cond:
            if revision > self._latest.get(zone_id, 0):
                self._latest[zone_id] = revision
            cond.notify_all()

    def wait_for_revision(
        self,
        zone_id: str,
        min_revision: int,
        timeout_ms: float = 5000,
    ) -> bool:
        """Wait until the zone revision reaches at least `min_revision`.

        Returns immediately if the cached latest revision already satisfies
        the target. Otherwise blocks on the per-zone Condition until either
        the revision arrives or the timeout expires.

        Args:
            zone_id: The zone to wait on.
            min_revision: The minimum acceptable revision.
            timeout_ms: Maximum wait time in milliseconds.

        Returns:
            True if the revision was reached, False on timeout.
        """
        cond = self._get_condition(zone_id)
        deadline = time.monotonic() + (timeout_ms / 1000)

        with cond:
            while True:
                if self._latest.get(zone_id, 0) >= min_revision:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                cond.wait(timeout=remaining)

    def get_latest_revision(self, zone_id: str) -> int:
        """Return the latest cached revision for a zone.

        Args:
            zone_id: The zone to query.

        Returns:
            The latest known revision, or 0 if no writes have been observed.
        """
        with self._guard:
            return self._latest.get(zone_id, 0)
