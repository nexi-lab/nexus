"""Permission lease table — check once, write many (Issue #3394).

Lightweight TTL-based cache that records successful permission checks.
On subsequent writes to the same (checked_path, agent_id) pair, the
cached grant is returned in ~100-200ns instead of performing a full
ReBAC check (~50-200μs).

This is deliberately NOT a LeaseManager.  Permission leases don't need
conflict resolution, fencing tokens, or revocation callbacks because
multiple agents can hold WRITE permission simultaneously (unlike file
locks).  A plain dict with TTL is the simplest correct implementation.

Keyed by the path that was *permission-checked*, not the file being
written.  For existing files this is the file path; for new files it's
the parent directory (since ``on_pre_write`` checks WRITE on the parent).
This design covers both repeated writes and many-files-in-same-directory.

Integration:
    - Checked in ``PermissionCheckHook.on_pre_write()`` (fast path)
    - Stamped after successful ReBAC check (slow path)
    - Invalidated by ``CacheCoordinator.invalidate_for_write()`` via
      registered callback (zone-wide clear on any permission mutation)

References:
    - Issue #3394: Permission write leases
    - Issue #3398: ReBAC permission leases for FUSE + multi-agent
    - DFUSE paper: https://arxiv.org/abs/2503.18191
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.lease import Clock

logger = logging.getLogger(__name__)

# Default clock that uses time.monotonic() directly — avoids importing
# from lib.lease at module level (which pulls in asyncio).
_DEFAULT_CLOCK: Clock | None = None


def _get_default_clock() -> Clock:
    """Lazy-import SystemClock to avoid circular import at module level."""
    global _DEFAULT_CLOCK  # noqa: PLW0603
    if _DEFAULT_CLOCK is None:
        from nexus.lib.lease import SystemClock

        _DEFAULT_CLOCK = SystemClock()
    return _DEFAULT_CLOCK


class PermissionLeaseTable:
    """TTL-based permission lease cache — check once, write many.

    Stores ``(checked_path, agent_id) -> expiry_monotonic`` in a plain
    dict.  Validation is a single dict lookup + float comparison (~100ns).

    Thread-safety: CPython's GIL protects dict reads/writes.  The worst
    case for a concurrent read during a write is a false negative (lease
    appears missing → falls through to full ReBAC check, which is safe).

    Memory: bounded by ``max_entries``.  When exceeded, the table is
    cleared entirely — equivalent to a cold start.

    Example::

        table = PermissionLeaseTable(ttl=30.0)
        # After successful ReBAC check:
        table.stamp("/workspace/src", "agent-A")
        # Fast path on next write:
        if table.check("/workspace/src", "agent-A"):
            return  # skip ReBAC check (~100ns vs ~50-200μs)
    """

    DEFAULT_TTL = 30.0
    DEFAULT_MAX_ENTRIES = 100_000

    def __init__(
        self,
        *,
        clock: "Clock | None" = None,
        ttl: float = DEFAULT_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._clock: Clock = clock or _get_default_clock()
        self._ttl = ttl
        self._max_entries = max_entries
        # (normalized_path, agent_id) -> monotonic expiry timestamp
        self._table: dict[tuple[str, str], float] = {}

        # Metrics
        self._hits = 0
        self._misses = 0
        self._stamps = 0
        self._invalidations = 0

    # -- path normalization ---------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        """Strip trailing slashes (except root) for consistent keying."""
        if path == "/":
            return path
        return path.rstrip("/")

    # -- core operations ------------------------------------------------------

    def check(self, path: str, agent_id: str) -> bool:
        """Check if a valid permission lease exists.

        O(1) dict lookup + monotonic time check.  Returns True if the
        agent has a non-expired lease on the given path.
        """
        expiry = self._table.get((self._normalize(path), agent_id))
        if expiry is not None and self._clock.monotonic() < expiry:
            self._hits += 1
            return True
        self._misses += 1
        return False

    def stamp(self, path: str, agent_id: str, ttl: float | None = None) -> None:
        """Record a successful permission check as a lease.

        Called after a full ReBAC check passes.  Subsequent writes to
        the same (path, agent_id) pair within the TTL skip the check.
        """
        # Size cap: clear all if above threshold (Decision #13D)
        if len(self._table) >= self._max_entries:
            logger.debug(
                "[PermissionLeaseTable] Size cap reached (%d), clearing all leases",
                len(self._table),
            )
            self._table.clear()
        self._table[(self._normalize(path), agent_id)] = self._clock.monotonic() + (
            ttl if ttl is not None else self._ttl
        )
        self._stamps += 1

    def invalidate_all(self) -> None:
        """Clear all leases (zone-wide revocation).

        Called by CacheCoordinator on any permission mutation.
        """
        if self._table:
            self._table.clear()
            self._invalidations += 1
            logger.debug("[PermissionLeaseTable] All leases invalidated")

    def invalidate_path(self, path: str) -> None:
        """Clear all leases for a specific checked path.

        Removes leases for all agents on the given path.  O(n) scan
        of the table — acceptable for targeted revocation but prefer
        ``invalidate_all()`` for zone-wide events.
        """
        normalized = self._normalize(path)
        keys_to_remove = [k for k in self._table if k[0] == normalized]
        for k in keys_to_remove:
            del self._table[k]
        if keys_to_remove:
            self._invalidations += 1

    # -- diagnostics ----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return operational metrics for monitoring."""
        return {
            "lease_hits": self._hits,
            "lease_misses": self._misses,
            "lease_stamps": self._stamps,
            "lease_invalidations": self._invalidations,
            "active_leases": len(self._table),
        }

    @property
    def active_count(self) -> int:
        """Number of entries in the table (including possibly expired)."""
        return len(self._table)
