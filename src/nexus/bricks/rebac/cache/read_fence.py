"""Read fence — per-zone generation counter for cross-zone staleness detection.

Each zone maintains a local generation counter that increments whenever the
durable stream consumer processes a cross-zone invalidation event.  The L1
cache stamps each entry with the fence generation at write time.  At read
time, if the fence generation has advanced past the entry's stamp, the entry
is stale and must be re-checked.

This avoids the clock-domain mismatch of comparing Redis Stream timestamps
against zone revisions — the generation is a single self-contained counter
that both the fence and the cache use.

Cost: one dict lookup + one int comparison per read (~50-100ns).

Related: Issue #3396 (decisions 2A, 13A)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ReadFence:
    """Per-zone generation counter for cache staleness detection.

    Thread-safety: CPython's GIL protects dict reads/writes and int
    increments.  Worst case for a concurrent read during an increment
    is reading the old generation (one event behind), which is safe —
    the next read will see the new generation.

    Usage::

        fence = ReadFence()

        # Consumer receives cross-zone invalidation:
        gen = fence.advance("zone-a")  # returns new generation

        # Cache stores entries with the generation at write time:
        entry.fence_generation = fence.generation("zone-a")

        # Read path checks:
        if fence.is_stale("zone-a", entry.fence_generation):
            # Re-check permission — a cross-zone revocation arrived
            ...
    """

    def __init__(self) -> None:
        # zone_id -> monotonically increasing generation counter
        self._generations: dict[str, int] = {}

        # Metrics
        self._advances = 0
        self._stale_hits = 0
        self._fresh_hits = 0

    def advance(self, zone_id: str) -> int:
        """Increment the generation for a zone.

        Called by the durable stream consumer when it processes a
        cross-zone invalidation event.  Always increments by 1.

        Returns:
            The new generation value.
        """
        gen = self._generations.get(zone_id, 0) + 1
        self._generations[zone_id] = gen
        self._advances += 1
        return gen

    def generation(self, zone_id: str) -> int:
        """Get the current generation for a zone.

        Used by the L1 cache to stamp entries at write time.
        Returns 0 if no invalidations have been received for this zone.
        """
        return self._generations.get(zone_id, 0)

    def is_stale(self, zone_id: str, cached_generation: int) -> bool:
        """Check if a cached result is stale relative to the zone generation.

        Args:
            zone_id: Zone to check.
            cached_generation: Generation at which the cache entry was written.

        Returns:
            True if a cross-zone invalidation has been received since
            the entry was cached (generation advanced).
            False if no invalidations since the entry was cached.
        """
        current = self._generations.get(zone_id, 0)
        if cached_generation < current:
            self._stale_hits += 1
            return True
        self._fresh_hits += 1
        return False

    def watermark(self, zone_id: str) -> int:
        """Get the current generation for a zone (diagnostics alias)."""
        return self._generations.get(zone_id, 0)

    def reset_zone(self, zone_id: str) -> None:
        """Reset generation for a zone (e.g. on zone leave)."""
        self._generations.pop(zone_id, None)

    def stats(self) -> dict[str, Any]:
        """Return operational metrics."""
        return {
            "zones_tracked": len(self._generations),
            "advances": self._advances,
            "stale_hits": self._stale_hits,
            "fresh_hits": self._fresh_hits,
            "watermarks": dict(self._generations),
        }
