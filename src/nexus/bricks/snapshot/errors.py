"""Snapshot domain errors (Issue #2131).

Hierarchy:
    TransactionConflictError   - Commit detects conflicting writes
    TransactionNotFoundError   - Transaction ID not found
    TransactionNotActiveError  - Operation targets non-active transaction
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.contracts.protocols.snapshot import ConflictInfo


class TransactionConflictError(Exception):
    """Raised when commit detects conflicting writes."""

    def __init__(self, conflicts: "list[ConflictInfo]") -> None:
        self.conflicts = conflicts
        paths = ", ".join(c.path for c in conflicts)
        super().__init__(f"Conflict detected on {len(conflicts)} path(s): {paths}")


class TransactionNotFoundError(Exception):
    """Raised when a transaction_id does not exist."""

    def __init__(self, transaction_id: str) -> None:
        self.transaction_id = transaction_id
        super().__init__(f"Transaction not found: {transaction_id}")


class TransactionNotActiveError(Exception):
    """Raised when an operation targets a non-active transaction."""

    def __init__(self, transaction_id: str, status: str) -> None:
        self.transaction_id = transaction_id
        self.status = status
        super().__init__(f"Transaction {transaction_id} is not active (status={status})")
