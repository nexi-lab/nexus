"""Condition-based revision notification for consistency tokens (Issue #1180 Phase B).

Provides a thread-safe notification mechanism for zone revisions (zookies).
Writers call notify_revision() after a successful write, and readers call
wait_for_revision() to block until the desired revision is available.

Lives in lib/ (tier-neutral utility) because it has zero nexus-specific
dependencies — only stdlib threading + abc.
"""

import logging
import threading
from abc import ABC, abstractmethod

from typing_extensions import override

logger = logging.getLogger(__name__)


class RevisionNotifierBase(ABC):
    """Abstract base for revision notification implementations.

    Provides a common interface so ruff auto-exempts ARG002 on overrides
    and isinstance() checks work at runtime.
    """

    @abstractmethod
    def notify_revision(self, zone_id: str, revision: int) -> None: ...

    @abstractmethod
    def get_latest_revision(self, zone_id: str) -> int: ...

    @abstractmethod
    def wait_for_revision(self, zone_id: str, min_revision: int, timeout_ms: int) -> bool: ...


class RevisionNotifier(RevisionNotifierBase):
    """Thread-safe revision notification using per-zone Condition variables.

    Each zone gets its own Condition so that notifications for zone A
    do not wake waiters in zone B.
    """

    def __init__(self) -> None:
        self._revisions: dict[str, int] = {}
        self._conditions: dict[str, threading.Condition] = {}
        self._lock = threading.Lock()

    def _get_condition(self, zone_id: str) -> "threading.Condition":
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


class NullRevisionNotifier(RevisionNotifierBase):
    """No-op fallback used when RevisionNotifier construction fails."""

    @override
    def notify_revision(self, zone_id: str, revision: int) -> None:  # noqa: ARG002
        pass

    @override
    def get_latest_revision(self, zone_id: str) -> int:  # noqa: ARG002
        return 0

    @override
    def wait_for_revision(
        self,
        zone_id: str,  # noqa: ARG002
        min_revision: int,  # noqa: ARG002
        timeout_ms: int,  # noqa: ARG002
    ) -> bool:
        return False
