"""Read fence — per-zone watermark for cross-zone staleness detection.

Each zone maintains a local watermark (monotonic sequence number from the
durable invalidation stream).  On every permission read, the fence checks
whether the cached result was computed before the latest revocation.

Cost: one dict lookup + one int comparison per read (~50-100ns).

The watermark is updated asynchronously by the durable stream consumer.
Reads never block on I/O.

Related: Issue #3396 (decisions 2A, 13A)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ReadFence:
    """Per-zone watermark for cache staleness detection.

    Thread-safety: CPython's GIL protects dict reads/writes.
    Worst case for a concurrent read during a write is reading
    a stale watermark (one event behind), which is safe — the
    next read will pick up the updated watermark.

    Example::

        fence = ReadFence()
        fence.advance("zone-a", 42)

        # On read path:
        if fence.is_stale("zone-a", cached_sequence=40):
            # Re-check permission — cache is behind the revocation
            ...
    """

    def __init__(self) -> None:
        # zone_id -> latest known durable stream sequence
        self._watermarks: dict[str, int] = {}

        # Metrics
        self._advances = 0
        self._stale_hits = 0
        self._fresh_hits = 0

    def advance(self, zone_id: str, sequence: int) -> None:
        """Advance the watermark for a zone.

        Called by the durable stream consumer when it processes an event.
        Only advances forward — never goes backward.

        Args:
            zone_id: Zone whose watermark to advance.
            sequence: Durable stream sequence number.
        """
        current = self._watermarks.get(zone_id, 0)
        if sequence > current:
            self._watermarks[zone_id] = sequence
            self._advances += 1

    def is_stale(self, zone_id: str, cached_sequence: int) -> bool:
        """Check if a cached result is stale relative to the zone watermark.

        Args:
            zone_id: Zone to check.
            cached_sequence: Sequence number at which the cached result
                was computed (e.g. from the zone's revision/zookie).

        Returns:
            True if the cached result is behind the latest known
            revocation for this zone (should re-check).
            False if the cached result is fresh.
        """
        watermark = self._watermarks.get(zone_id, 0)
        if cached_sequence < watermark:
            self._stale_hits += 1
            return True
        self._fresh_hits += 1
        return False

    def watermark(self, zone_id: str) -> int:
        """Get the current watermark for a zone (diagnostics)."""
        return self._watermarks.get(zone_id, 0)

    def reset_zone(self, zone_id: str) -> None:
        """Reset watermark for a zone (e.g. on zone leave)."""
        self._watermarks.pop(zone_id, None)

    def stats(self) -> dict[str, Any]:
        """Return operational metrics."""
        return {
            "zones_tracked": len(self._watermarks),
            "advances": self._advances,
            "stale_hits": self._stale_hits,
            "fresh_hits": self._fresh_hits,
            "watermarks": dict(self._watermarks),
        }
