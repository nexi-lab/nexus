"""Snapshot Brick — Transactional Filesystem Snapshots (Issue #1752, #2131).

Self-contained brick for transactional filesystem snapshots with
begin/commit/rollback semantics and MVCC conflict detection.

Public API:
    TransactionalSnapshotService  - Main service
    TransactionRegistry           - In-memory fast-path registry
    SnapshotCleanupWorker         - Background cleanup worker

Errors:
    TransactionConflictError      - Commit detects conflicting writes
    TransactionNotFoundError      - Transaction ID not found
    TransactionNotActiveError     - Operation targets non-active transaction
"""

from nexus.bricks.snapshot.errors import TransactionConflictError as TransactionConflictError
from nexus.bricks.snapshot.errors import TransactionNotActiveError as TransactionNotActiveError
from nexus.bricks.snapshot.errors import TransactionNotFoundError as TransactionNotFoundError
