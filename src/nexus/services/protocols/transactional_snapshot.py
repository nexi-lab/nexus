"""Transactional snapshot protocol (Issue #1752).

Defines the contract for atomic COW filesystem snapshots that enable
agent rollback of risky operations.

Architecture: System Service (Tier 2) per NEXUS-LEGO-ARCHITECTURE.md.
Triggered via KernelDispatch INTERCEPT on destructive VFS ops.

Transaction lifecycle (strict state machine):
    begin()  -> ACTIVE
                ├── commit()   -> COMMITTED   (terminal)
                ├── rollback() -> ROLLED_BACK  (terminal)
                └── TTL expiry -> EXPIRED      (terminal, auto-rollback)

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §2 (System Services tier)
    - Fault-Tolerant Sandboxing for AI Coding Agents (Dec 2025)
    - Issue #1752: Transactional filesystem snapshots for agent rollback
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import SnapshotId

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TransactionState(StrEnum):
    """Transaction lifecycle states (strict state machine)."""

    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    """Snapshot of a single path's state at begin() time.

    Attributes:
        path: Virtual path that was snapshotted.
        content_hash: CAS hash of the content (None if path didn't exist).
        size: File size in bytes (0 if absent).
        metadata_json: Serialized metadata at snapshot time (None if absent).
        existed: Whether the path existed at snapshot time.
    """

    path: str
    content_hash: str | None
    size: int
    metadata_json: str | None
    existed: bool


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    """Information about a path that couldn't be cleanly rolled back.

    Attributes:
        path: The conflicting path.
        snapshot_hash: Content hash at begin() time.
        current_hash: Content hash at rollback() time.
        reason: Human-readable explanation.
    """

    path: str
    snapshot_hash: str | None
    current_hash: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """Result of a rollback() operation.

    Attributes:
        snapshot_id: The transaction that was rolled back.
        reverted: Paths successfully restored to snapshot state.
        conflicts: Paths with concurrent modifications that were NOT reverted.
        deleted: Paths deleted (were absent at snapshot time).
        stats: Summary statistics.
    """

    snapshot_id: str
    reverted: list[str] = field(default_factory=list)
    conflicts: list[ConflictInfo] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransactionInfo:
    """Read-only view of a transaction's state.

    Attributes:
        snapshot_id: Unique identifier.
        agent_id: Agent that owns this transaction.
        zone_id: Zone scope.
        status: Current lifecycle state.
        paths: Paths included in the snapshot.
        created_at: When begin() was called.
        expires_at: When the transaction will auto-expire.
        committed_at: When commit() was called (None if not committed).
        rolled_back_at: When rollback() was called (None if not rolled back).
    """

    snapshot_id: str
    agent_id: str
    zone_id: str
    status: TransactionState
    paths: list[str]
    created_at: str
    expires_at: str
    committed_at: str | None = None
    rolled_back_at: str | None = None


@dataclass(frozen=True, slots=True)
class TransactionConfig:
    """Configuration for the transactional snapshot service.

    Attributes:
        ttl_seconds: Time-to-live for ACTIVE transactions before auto-expiry.
        max_paths_per_transaction: Maximum number of paths per transaction.
        auto_snapshot_on_destructive: Whether to auto-snapshot on PRE_WRITE/PRE_DELETE hooks.
        cleanup_interval_seconds: How often to run TTL cleanup.
    """

    ttl_seconds: int = 3600
    max_paths_per_transaction: int = 10_000
    auto_snapshot_on_destructive: bool = False
    cleanup_interval_seconds: int = 60


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidTransactionStateError(Exception):
    """Raised when a transaction operation is invalid for the current state."""

    def __init__(self, snapshot_id: str, current_state: TransactionState, attempted_action: str):
        self.snapshot_id = snapshot_id
        self.current_state = current_state
        self.attempted_action = attempted_action
        super().__init__(
            f"Cannot {attempted_action} transaction {snapshot_id}: current state is {current_state}"
        )


class TransactionNotFoundError(Exception):
    """Raised when a transaction snapshot is not found."""

    def __init__(self, snapshot_id: str):
        self.snapshot_id = snapshot_id
        super().__init__(f"Transaction snapshot not found: {snapshot_id}")


class OverlappingTransactionError(Exception):
    """Raised when begin() is called with paths already in an active transaction."""

    def __init__(self, agent_id: str, overlapping_paths: list[str]):
        self.agent_id = agent_id
        self.overlapping_paths = overlapping_paths
        super().__init__(
            f"Agent {agent_id} already has active transaction on paths: "
            f"{overlapping_paths[:5]}{'...' if len(overlapping_paths) > 5 else ''}"
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TransactionalSnapshotProtocol(Protocol):
    """Service contract for transactional filesystem snapshots.

    Enables atomic COW snapshots before risky agent operations,
    with optimistic concurrency and conflict reporting on rollback.
    """

    async def begin(
        self,
        agent_id: str,
        paths: list[str],
        *,
        zone_id: str = ROOT_ZONE_ID,
        context: "OperationContext | None" = None,
    ) -> SnapshotId:
        """Create a COW snapshot of specified paths.

        Records current metadata + content hashes for each path.
        Paths that don't exist are recorded as absent (rollback = delete).

        Args:
            agent_id: Agent initiating the transaction.
            paths: Virtual paths to snapshot (must be non-empty).
            zone_id: Zone scope for the transaction.
            context: Operation context for permission checks.

        Returns:
            Opaque snapshot ID for commit/rollback.

        Raises:
            ValueError: If paths is empty or exceeds max_paths_per_transaction.
            OverlappingTransactionError: If agent has active transaction on any path.
            PermissionError: If agent lacks permission on any path.
        """
        ...

    async def commit(
        self,
        snapshot_id: SnapshotId,
        *,
        context: "OperationContext | None" = None,
    ) -> None:
        """Release snapshot — changes are permanent.

        Transitions transaction from ACTIVE to COMMITTED.

        Args:
            snapshot_id: Transaction to commit.
            context: Operation context for permission checks.

        Raises:
            TransactionNotFoundError: If snapshot doesn't exist.
            InvalidTransactionStateError: If not in ACTIVE state.
        """
        ...

    async def rollback(
        self,
        snapshot_id: SnapshotId,
        *,
        context: "OperationContext | None" = None,
    ) -> TransactionResult:
        """Restore all paths to pre-snapshot state.

        Uses optimistic concurrency: if another agent modified a path
        since begin(), it's reported as a conflict and NOT reverted.

        Args:
            snapshot_id: Transaction to rollback.
            context: Operation context for permission checks.

        Returns:
            TransactionResult with reverted paths, conflicts, and stats.

        Raises:
            TransactionNotFoundError: If snapshot doesn't exist.
            InvalidTransactionStateError: If not in ACTIVE state.
        """
        ...

    async def get_transaction(
        self,
        snapshot_id: SnapshotId,
    ) -> TransactionInfo:
        """Get transaction details (read-only).

        Args:
            snapshot_id: Transaction to look up.

        Returns:
            TransactionInfo with current state and metadata.

        Raises:
            TransactionNotFoundError: If snapshot doesn't exist.
        """
        ...

    async def list_active(
        self,
        agent_id: str,
        *,
        zone_id: str = ROOT_ZONE_ID,
    ) -> list[TransactionInfo]:
        """List all ACTIVE transactions for an agent.

        Args:
            agent_id: Agent to query.
            zone_id: Zone scope.

        Returns:
            List of active TransactionInfo, ordered by created_at DESC.
        """
        ...

    async def cleanup_expired(self) -> int:
        """Expire and auto-rollback transactions past their TTL.

        Returns:
            Number of transactions expired.
        """
        ...
