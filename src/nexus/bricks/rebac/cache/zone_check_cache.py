"""Revision-validated cache for zone-aware single permission checks.

The zone-aware ``rebac_check`` path lost its result cache when the L2 SQL
cache (``rebac_check_cache``) was removed: its cache hooks became no-ops, so
every repeated check re-walked the graph with several Postgres round trips.

The shared L1 ``ReBACPermissionCache`` is not a safe drop-in here — it is
invalidated by exact subject/object only (a group or parent-directory change
leaves derived decisions cached), and its entries are stamped with the
revision read AFTER computing.  This cache instead serves a decision only
while the zone's tuple revision still equals the revision read BEFORE the
decision was computed.  Every tuple mutation bumps the zone revision, so any
write in the zone retires every earlier decision — exactly as fresh as an
uncached check — while repeated checks between writes skip the traversal.
The stamp also carries the manager's in-process tuple version, which path
renames bump without touching the zone revision.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

CheckKey = tuple[str, str, str, str, str, str]
"""(zone_id, subject_type, subject_id, permission, object_type, object_id)"""

Revision = tuple[int, int]
"""(zone tuple revision, process tuple version) read before computing."""


class ZoneRevisionCheckCache:
    """Bounded LRU of ``key -> (revision, allowed, cached_at)``."""

    def __init__(self, *, max_entries: int = 50_000, ttl_seconds: float = 300.0) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[CheckKey, tuple[Revision, bool, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: CheckKey, revision: Revision) -> bool | None:
        """Cached decision if it was computed at exactly ``revision``."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            cached_revision, allowed, cached_at = entry
            if cached_revision != revision or now - cached_at > self._ttl_seconds:
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return allowed

    def put(self, key: CheckKey, revision: Revision, allowed: bool) -> None:
        """Record a decision computed against the tuples at ``revision``."""
        with self._lock:
            self._entries[key] = (revision, allowed, time.monotonic())
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
