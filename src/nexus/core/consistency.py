"""Filesystem consistency levels and zone-level replication modes.

Issue #923: Per-operation consistency (FSConsistency) — controls read freshness via zookies.
Issue #1180: Per-zone replication mode (ConsistencyMode) — controls Raft SC vs EC.
Issue #1180: Store operational mode (StoreMode) — tracks RaftMetadataStore mode.

These are orthogonal dimensions. FSConsistency controls *how fresh* a read must be.
ConsistencyMode controls *how writes are replicated*. The COMPATIBILITY_MATRIX
defines behavior for each (ConsistencyMode × FSConsistency) combination.

Usage:
    from nexus.core.consistency import FSConsistency, ConsistencyMode, StoreMode

    # Per-operation consistency (on OperationContext)
    ctx = OperationContext(
        user="alice",
        groups=[],
        consistency=FSConsistency.STRONG,
        min_zookie=zookie_from_write,
    )
    content = fs.read("/file.txt", context=ctx)

    # Per-zone replication mode (on ZoneModel.consistency_mode)
    zone.consistency_mode = ConsistencyMode.EC  # Eventual consistency

See also:
    - Issue #916: ZedToken consistency for permissions (complementary)
    - Issue #1187: Zookie consistency tokens (foundation)
    - https://man7.org/linux/man-pages/man5/nfs.5.html (NFS CTO)
    - docs/architecture/federation-memo.md §4.5 (Raft dual mode)
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


class ConsistencyMode(StrEnum):
    """Zone-level Raft replication mode (Issue #1180).

    Controls how writes are replicated across Raft nodes.
    Orthogonal to FSConsistency (per-operation read freshness).
    Stored in ZoneModel.consistency_mode column.

    See COMPATIBILITY_MATRIX for interaction with FSConsistency.
    See docs/architecture/federation-memo.md §4.5 for design rationale.
    """

    SC = "SC"
    """Strong Consistency — Raft consensus on every write.

    - Writes: Go through Raft propose → majority ACK → commit
    - Reads: Linearizable (Leader Read or Read Index)
    - Latency: ~5-10ms intra-DC, ~50-100ms cross-region
    - Throughput: ~1K writes/sec
    - Use for: Financial, legal, compliance workloads
    """

    EC = "EC"
    """Eventual Consistency — local apply + async replication.

    - Writes: Apply locally (~5μs), replicate in background via Raft
    - Reads: May observe stale data (bounded staleness)
    - Latency: ~1-2ms (local sled read)
    - Throughput: ~30K writes/sec
    - Risk: Data loss on leader crash before replication completes
    - Use for: Media, content delivery, high-throughput ingestion
    """


# Default zone replication mode
DEFAULT_CONSISTENCY_MODE = ConsistencyMode.SC


class StoreMode(StrEnum):
    """RaftMetadataStore operational mode (Issue #1180).

    Tracks how the metadata store was initialized. Used for mode-aware
    branching (e.g., compatibility matrix checks, error messages, monitoring).

    Set by factory methods: .embedded(), .sc(), .ec(), .remote().
    """

    EMBEDDED = "embedded"
    """Direct sled via Metastore PyO3 (~5μs). No replication."""

    SC = "sc"
    """Raft consensus via RaftConsensus PyO3 (~2-10ms). Synchronous replication."""

    EC = "ec"
    """Lazy consensus via RaftConsensus PyO3 (~5μs). Async background replication."""

    REMOTE = "remote"
    """gRPC thin client (~200μs). Delegates to remote Raft node."""


class MigrationState(StrEnum):
    """State machine for zone consistency mode migration (Issue #1180 Phase C).

    Lifecycle: IDLE → DRAINING → QUIESCED → SWITCHING → VALIDATING → IDLE
    On failure at any step: → FAILED (rollback to previous mode).
    """

    IDLE = "idle"
    """No migration in progress."""

    DRAINING = "draining"
    """Waiting for in-flight writes to complete."""

    QUIESCED = "quiesced"
    """All writes paused; zone is quiescent."""

    SWITCHING = "switching"
    """Changing the consistency mode in DB and Raft."""

    VALIDATING = "validating"
    """Verifying the new mode is operational."""

    FAILED = "failed"
    """Migration failed; rolled back to previous mode."""


# Valid migration transitions: (from_mode, to_mode) → migration strategy name
VALID_MIGRATIONS: dict[tuple[ConsistencyMode, ConsistencyMode], str] = {
    (ConsistencyMode.SC, ConsistencyMode.EC): "sc_to_ec",
    (ConsistencyMode.EC, ConsistencyMode.SC): "ec_to_sc",
}


def validate_migration(
    current: ConsistencyMode, target: ConsistencyMode
) -> tuple[bool, str | None]:
    """Check whether a migration from `current` to `target` is allowed.

    Args:
        current: The current zone consistency mode.
        target: The desired target consistency mode.

    Returns:
        (True, None) if valid, (False, error_message) if invalid.
    """
    if current == target:
        return False, f"Zone is already in {current.value} mode"
    if (current, target) not in VALID_MIGRATIONS:
        return False, (
            f"Migration from {current.value} to {target.value} is not supported. "
            f"Valid migrations: {', '.join(f'{a.value}→{b.value}' for a, b in VALID_MIGRATIONS)}"
        )
    return True, None


# Compatibility matrix: (ConsistencyMode, FSConsistency) → behavior
# Defines how per-zone replication mode interacts with per-operation read consistency.
#
# Behaviors:
#   "skip_zookie_wait"  — No zookie check, return cached/local data immediately
#   "wait_best_effort"  — Wait for zookie revision; on timeout, fall through silently
#   "wait_or_raise"     — Wait for zookie revision; on timeout, raise ConsistencyTimeoutError
#   "warn_then_wait"    — Log warning (STRONG on EC is misleading), then wait_best_effort
COMPATIBILITY_MATRIX: dict[tuple[ConsistencyMode, FSConsistency], str] = {
    (ConsistencyMode.SC, FSConsistency.EVENTUAL): "skip_zookie_wait",
    (ConsistencyMode.SC, FSConsistency.CLOSE_TO_OPEN): "wait_best_effort",
    (ConsistencyMode.SC, FSConsistency.STRONG): "wait_or_raise",
    (ConsistencyMode.EC, FSConsistency.EVENTUAL): "skip_zookie_wait",
    (ConsistencyMode.EC, FSConsistency.CLOSE_TO_OPEN): "wait_best_effort",
    (ConsistencyMode.EC, FSConsistency.STRONG): "warn_then_wait",
}
