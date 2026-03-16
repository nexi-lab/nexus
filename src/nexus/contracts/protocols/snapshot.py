"""Snapshot service protocol for transactional filesystem rollback (Issue #1752).

Defines the contract and frozen data models for transactional filesystem snapshots.
Agents can begin a transaction, perform writes/deletes, and atomically commit
or rollback to the pre-transaction state.

Follows patterns from brick_lifecycle.py.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Data models (frozen — immutable value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransactionInfo:
    """Immutable snapshot of a transaction's state.

    Attributes:
        transaction_id: Unique transaction identifier (UUID).
        zone_id: Zone scope for the transaction.
        agent_id: Agent that owns the transaction (if any).
        status: Current lifecycle state (active/committed/rolled_back/expired).
        description: Optional human-readable description.
        created_at: When the transaction was created.
        expires_at: When the transaction will auto-expire.
        entry_count: Number of tracked file operations.
    """

    transaction_id: str
    zone_id: str
    agent_id: str | None
    status: str
    description: str | None
    created_at: datetime
    expires_at: datetime
    entry_count: int


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """Immutable record of a single tracked file operation.

    Attributes:
        entry_id: Unique entry identifier (UUID).
        transaction_id: Parent transaction ID.
        path: Virtual path of the affected file.
        operation: Type of operation (write/delete/rename).
        original_hash: CAS hash of the file before the operation (None for new files).
        original_metadata: JSON-serialized metadata before the operation.
        new_hash: CAS hash after the operation (for conflict detection).
        created_at: When the entry was recorded.
    """

    entry_id: str
    transaction_id: str
    path: str
    operation: str
    original_hash: str | None
    original_metadata: str | None
    new_hash: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    """Information about a detected conflict during commit.

    Attributes:
        path: Virtual path where the conflict was detected.
        expected_hash: Hash the transaction expected (snapshot at track time).
        current_hash: Current hash in the filesystem.
        reason: Human-readable explanation of the conflict.
    """

    path: str
    expected_hash: str | None
    current_hash: str | None
    reason: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SnapshotServiceProtocol(Protocol):
    """Service contract for transactional filesystem snapshots.

    Async methods are used for DB-backed operations.
    Sync methods (track_write, track_delete, is_tracked) are called
    from the synchronous write path in nexus_fs.py.
    """

    async def begin(
        self,
        zone_id: str,
        agent_id: str | None = None,
        description: str | None = None,
        ttl_seconds: int = 3600,
    ) -> TransactionInfo: ...

    async def commit(self, transaction_id: str) -> TransactionInfo: ...

    async def rollback(self, transaction_id: str) -> TransactionInfo: ...

    async def get_transaction(self, transaction_id: str) -> TransactionInfo | None: ...

    async def list_transactions(
        self,
        zone_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TransactionInfo]: ...

    async def list_entries(self, transaction_id: str) -> list[SnapshotEntry]: ...

    # Sync methods (called from sync write path)
    def track_write(
        self,
        transaction_id: str,
        path: str,
        original_hash: str | None,
        original_metadata: dict[str, Any] | None,
        new_hash: str | None,
    ) -> None: ...

    def track_delete(
        self,
        transaction_id: str,
        path: str,
        original_hash: str | None,
        original_metadata: dict[str, Any] | None,
    ) -> None: ...

    def is_tracked(self, path: str) -> str | None: ...
