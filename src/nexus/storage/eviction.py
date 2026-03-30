"""Shared eviction predicates for lease-aware cache eviction (Issue #3400).

Provides a single, testable function that encodes the eviction-candidate
rules for caches that support lease-aware eviction.  Currently consumed
by ``FileContentCache``; the predicate is designed so that ``LocalDiskCache``
(CLOCK) and ``ContentCache`` (LRU) can adopt it when extended to path-keyed
lease awareness in the future.

Design references:
    - CephFS: capability bits cached locally, updated via MDS callbacks
    - NFSv4: revoke-then-evict — never silently evict leased entries
    - DFUSE §3.2: lease-coordinated invalidation
"""


def is_eviction_candidate(
    *,
    has_active_lease: bool,
    priority: int,
    pass_number: int,
) -> bool:
    """Determine if a cache entry is eligible for eviction.

    Three-pass escalation:

    Pass 1
        Conservative — only entries with **no** active lease **and**
        ``priority == 0``.  This preserves both leased data and
        high-priority data.

    Pass 2
        Moderate — entries with **no** active lease, regardless of
        priority.  High-priority entries are sacrificed only when all
        low-priority unleased entries have been exhausted.

    Pass 3+
        Emergency — any entry is a candidate.  The caller **must**
        first revoke the lease (decision 2A: revoke-then-evict) so
        that ``has_active_lease`` is False by the time eviction
        proceeds.  If a lease is still active, the entry is still
        eligible (hard fallback), but this path should never be
        reached under normal operation.

    Args:
        has_active_lease: True if the entry has a currently valid lease.
        priority: Non-negative integer; higher = more important.
        pass_number: Eviction pass (1-based, ≥ 1).

    Returns:
        True if the entry may be evicted in the given pass.
    """
    if pass_number <= 1:
        return not has_active_lease and priority == 0
    if pass_number == 2:
        return not has_active_lease
    # Pass 3+: emergency — everything is a candidate
    return True
