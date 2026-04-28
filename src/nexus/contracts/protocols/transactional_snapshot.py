"""Transactional snapshot protocol (Issue #1752).

Defines the contract for atomic COW filesystem snapshots that enable
agent rollback of risky operations.

Triggered via KernelDispatch INTERCEPT on destructive VFS ops.

Transaction lifecycle (strict state machine):
    begin()  -> ACTIVE
                ├── commit()   -> COMMITTED   (terminal)
                ├── rollback() -> ROLLED_BACK  (terminal)
                └── TTL expiry -> EXPIRED      (terminal, auto-rollback)

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §2
    - Fault-Tolerant Sandboxing for AI Coding Agents (Dec 2025)
    - Issue #1752: Transactional filesystem snapshots for agent rollback
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

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
        content_id: CAS hash of the content (None if path didn't exist).
        size: File size in bytes (0 if absent).
        metadata_json: Serialized metadata at snapshot time (None if absent).
        existed: Whether the path existed at snapshot time.
    """

    path: str
    content_id: str | None
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
        zone_id: str,
        agent_id: str | None = None,
        description: str | None = None,
        ttl_seconds: int = 3600,
    ) -> TransactionInfo:
        """Begin a new transaction.

        Creates a snapshot record with status ACTIVE and registers
        the transaction in the in-memory registry.

        Args:
            zone_id: Zone scope for the transaction.
            agent_id: Agent initiating the transaction (optional).
            description: Human-readable description (optional).
            ttl_seconds: Time-to-live before auto-expiry.

        Returns:
            TransactionInfo with the new transaction's metadata.

        Raises:
            OverlappingTransactionError: If agent has active transaction on any path.
        """
        ...

    async def commit(
        self,
        transaction_id: str,
    ) -> TransactionInfo:
        """Release snapshot — changes are permanent.

        Checks each entry's new_hash against the current file state.
        Transitions transaction from ACTIVE to COMMITTED.

        Args:
            transaction_id: Transaction to commit.

        Returns:
            TransactionInfo with updated status.

        Raises:
            TransactionNotFoundError: If transaction doesn't exist.
            InvalidTransactionStateError: If not in ACTIVE state.
        """
        ...

    async def rollback(
        self,
        transaction_id: str,
    ) -> TransactionInfo:
        """Restore all paths to pre-transaction state.

        Processes entries in reverse order (LIFO) to handle dependent
        operations.  Uses optimistic concurrency: if another agent modified
        a path since begin(), it's reported as a conflict and NOT reverted.

        Args:
            transaction_id: Transaction to rollback.

        Returns:
            TransactionInfo with updated status.

        Raises:
            TransactionNotFoundError: If transaction doesn't exist.
            InvalidTransactionStateError: If not in ACTIVE state.
        """
        ...

    async def get_transaction(
        self,
        transaction_id: str,
    ) -> TransactionInfo:
        """Get transaction details (read-only).

        Args:
            transaction_id: Transaction to look up.

        Returns:
            TransactionInfo with current state and metadata.

        Raises:
            TransactionNotFoundError: If transaction doesn't exist.
        """
        ...

    async def cleanup_expired(self) -> int:
        """Expire and auto-rollback transactions past their TTL.

        Returns:
            Number of transactions expired.
        """
        ...
