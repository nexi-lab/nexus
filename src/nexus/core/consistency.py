"""Filesystem consistency levels for close-to-open consistency model (Issue #923).

Inspired by JuiceFS close-to-open consistency and NFS CTO cache coherence.
Provides three levels of consistency for filesystem metadata operations:

- EVENTUAL: Fastest. May return stale metadata. Use for bulk reads.
- CLOSE_TO_OPEN: Default. If a zookie is provided from a prior write,
  waits for that revision before reading. Otherwise same as EVENTUAL.
- STRONG: Slowest. Bypasses metadata cache. Raises on timeout.

Usage:
    from nexus.core.consistency import FSConsistency

    # Set on OperationContext
    ctx = OperationContext(
        user="alice",
        groups=[],
        consistency=FSConsistency.STRONG,
        min_zookie=zookie_from_write,
    )
    content = fs.read("/file.txt", context=ctx)

See also:
    - Issue #916: ZedToken consistency for permissions (complementary)
    - Issue #1187: Zookie consistency tokens (foundation)
    - https://man7.org/linux/man-pages/man5/nfs.5.html (NFS CTO)
"""

from __future__ import annotations

from enum import StrEnum


class FSConsistency(StrEnum):
    """Filesystem operation consistency levels.

    Controls the tradeoff between read latency and data freshness
    for metadata operations (path -> etag mapping).

    Note: Content addressed by hash (CAS) is always consistent.
    This enum controls metadata freshness only.
    """

    EVENTUAL = "eventual"
    """May see stale metadata. Fastest option.

    - Read: Returns cached metadata if available
    - Write: Normal behavior (always returns zookie)
    - Use for: Bulk reads where staleness is acceptable
    """

    CLOSE_TO_OPEN = "close_to_open"
    """Changes visible after operation completes. Default.

    - Read: If min_zookie provided, waits for that revision (best-effort).
            On timeout, falls through to eventual behavior.
    - Write: Normal behavior (always returns zookie)
    - Use for: Normal operations (JuiceFS default)
    """

    STRONG = "strong"
    """Immediately consistent. Slowest option.

    - Read: If min_zookie provided, waits for that revision.
            On timeout, raises ConsistencyTimeoutError.
    - Write: Normal behavior (always returns zookie)
    - Use for: Critical operations where freshness is required
    """


# Default consistency level (matches JuiceFS default)
DEFAULT_CONSISTENCY = FSConsistency.CLOSE_TO_OPEN
