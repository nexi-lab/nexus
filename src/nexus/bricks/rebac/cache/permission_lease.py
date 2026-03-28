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

Inheritance-aware: ``check()`` walks up the path hierarchy so a lease
stamped on a parent directory (e.g. from a new-file write) also covers
writes to existing files in that directory.  This matches the ReBAC
inheritance model where WRITE on a directory implies WRITE on children.

Integration:
    - Checked in ``PermissionCheckHook.on_pre_write/read/delete/rmdir()``
      (fast path)
    - Stamped after successful ReBAC check (slow path)
    - Invalidated by ``CacheCoordinator.invalidate_for_write()`` via
      registered callback (path-targeted for direct grants, zone-wide
      fallback for group/inherited changes)
    - ``invalidate_agent()`` called on agent termination / permission change

References:
    - Issue #3394: Permission write leases
    - Issue #3398: ReBAC permission leases for FUSE + multi-agent
    - DFUSE paper: https://arxiv.org/abs/2503.18191
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.lib.path_utils import parent_path

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

    Secondary indexes ``_by_path`` and ``_by_agent`` enable O(1) targeted
    invalidation for path-specific revocation (Issue #3398 decision 3A)
    and agent termination cleanup (Issue #3398 decision 2A).

    Thread-safety: CPython's GIL protects dict reads/writes.  The worst
    case for a concurrent read during a write is a false negative (lease
    appears missing → falls through to full ReBAC check, which is safe).

    Memory: bounded by ``max_entries``.  When at 90% capacity, expired
    entries are lazily evicted.  If still over cap after eviction, the
    table is cleared entirely — equivalent to a cold start.

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
    _EVICTION_THRESHOLD = 0.9  # trigger lazy eviction at 90% capacity

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

        # Secondary indexes for O(1) targeted invalidation (Issue #3398)
        # path -> set of agent_ids that hold leases on this path
        self._by_path: dict[str, set[str]] = {}
        # agent_id -> set of paths that this agent holds leases on
        self._by_agent: dict[str, set[str]] = {}

        # Metrics
        self._hits = 0
        self._misses = 0
        self._stamps = 0
        self._invalidations = 0
        self._evictions = 0

    # -- path helpers ----------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        """Strip trailing slashes (except root) for consistent keying."""
        if path == "/":
            return path
        return path.rstrip("/")

    # -- secondary index maintenance -------------------------------------------

    def _index_add(self, normalized_path: str, agent_id: str) -> None:
        """Add an entry to secondary indexes."""
        by_path = self._by_path.get(normalized_path)
        if by_path is None:
            by_path = set()
            self._by_path[normalized_path] = by_path
        by_path.add(agent_id)

        by_agent = self._by_agent.get(agent_id)
        if by_agent is None:
            by_agent = set()
            self._by_agent[agent_id] = by_agent
        by_agent.add(normalized_path)

    def _index_remove(self, normalized_path: str, agent_id: str) -> None:
        """Remove an entry from secondary indexes."""
        by_path = self._by_path.get(normalized_path)
        if by_path is not None:
            by_path.discard(agent_id)
            if not by_path:
                del self._by_path[normalized_path]

        by_agent = self._by_agent.get(agent_id)
        if by_agent is not None:
            by_agent.discard(normalized_path)
            if not by_agent:
                del self._by_agent[agent_id]

    def _index_clear(self) -> None:
        """Clear all secondary indexes."""
        self._by_path.clear()
        self._by_agent.clear()

    # -- core operations -------------------------------------------------------

    def check(self, path: str, agent_id: str) -> bool:
        """Check if a valid permission lease exists.

        Walks up the path hierarchy (inheritance-aware): a lease stamped
        on an ancestor directory covers writes to descendant paths.  This
        matches the ReBAC model where WRITE on a directory implies WRITE
        on children via ``parent-of`` relationships.

        Typical cost: O(depth) dict lookups where depth ≈ 3-5.  Each
        lookup is ~50-100ns, total ~250-500ns on miss — well within the
        ~1μs budget.
        """
        now = self._clock.monotonic()
        current: str | None = self._normalize(path)
        while current is not None:
            expiry = self._table.get((current, agent_id))
            if expiry is not None and now < expiry:
                self._hits += 1
                return True
            current = parent_path(current)
        self._misses += 1
        return False

    def stamp(self, path: str, agent_id: str, ttl: float | None = None) -> None:
        """Record a successful permission check as a lease.

        Called after a full ReBAC check passes.  Subsequent writes to
        the same (path, agent_id) pair within the TTL skip the check.
        """
        normalized = self._normalize(path)
        # Lazy eviction at 90% capacity (Issue #3398 decision 15A)
        if len(self._table) >= int(self._max_entries * self._EVICTION_THRESHOLD):
            self._evict_expired()
            # Full clear only if still over cap after eviction
            if len(self._table) >= self._max_entries:
                logger.debug(
                    "[PermissionLeaseTable] Size cap reached (%d) after eviction, clearing all",
                    len(self._table),
                )
                self._table.clear()
                self._index_clear()

        key = (normalized, agent_id)
        is_new = key not in self._table
        self._table[key] = self._clock.monotonic() + (ttl if ttl is not None else self._ttl)
        if is_new:
            self._index_add(normalized, agent_id)
        self._stamps += 1

    def _evict_expired(self) -> None:
        """Remove expired entries from the table and indexes.

        Called lazily when table approaches capacity.  O(n) scan but
        only triggers at 90% capacity — not on every stamp.
        """
        now = self._clock.monotonic()
        expired_keys = [k for k, v in self._table.items() if v <= now]
        for key in expired_keys:
            del self._table[key]
            self._index_remove(key[0], key[1])
        if expired_keys:
            self._evictions += len(expired_keys)
            logger.debug(
                "[PermissionLeaseTable] Evicted %d expired entries, %d remain",
                len(expired_keys),
                len(self._table),
            )

    def invalidate_all(self) -> None:
        """Clear all leases (zone-wide revocation).

        Called by CacheCoordinator on group/inherited permission mutations.
        """
        if self._table:
            self._table.clear()
            self._index_clear()
            self._invalidations += 1
            logger.debug("[PermissionLeaseTable] All leases invalidated")

    def invalidate_path(self, path: str) -> None:
        """Clear all leases for a specific checked path.

        Uses the ``_by_path`` secondary index for O(k) targeted removal
        where k = number of agents holding leases on this path (typically
        1-5).  Used for direct-grant permission mutations (Issue #3398
        decision 3A).
        """
        normalized = self._normalize(path)
        agent_ids = self._by_path.get(normalized)
        if not agent_ids:
            return
        # Copy because we mutate during iteration
        for aid in list(agent_ids):
            key = (normalized, aid)
            self._table.pop(key, None)
            self._index_remove(normalized, aid)
        self._invalidations += 1

    def invalidate_agent(self, agent_id: str) -> None:
        """Clear all leases for a specific agent.

        Uses the ``_by_agent`` secondary index for O(k) targeted removal
        where k = number of paths this agent holds leases on.
        Called on agent termination or agent-level permission change
        (Issue #3398 decision 2A).
        """
        paths = self._by_agent.get(agent_id)
        if not paths:
            return
        count = 0
        # Copy because we mutate during iteration
        for p in list(paths):
            key = (p, agent_id)
            if self._table.pop(key, None) is not None:
                count += 1
            self._index_remove(p, agent_id)
        if count:
            self._invalidations += 1
            logger.debug(
                "[PermissionLeaseTable] Invalidated %d lease(s) for agent %s",
                count,
                agent_id,
            )

    # -- diagnostics -----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return operational metrics for monitoring."""
        return {
            "lease_hits": self._hits,
            "lease_misses": self._misses,
            "lease_stamps": self._stamps,
            "lease_invalidations": self._invalidations,
            "lease_evictions": self._evictions,
            "active_leases": len(self._table),
        }

    @property
    def active_count(self) -> int:
        """Number of entries in the table (including possibly expired)."""
        return len(self._table)
