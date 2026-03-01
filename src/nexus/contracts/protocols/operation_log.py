"""Operation log service protocol (ops-scenario-matrix S24: Operations Undo).

Defines the contract for operation logging — tracking filesystem operations
for audit trail, undo capability, and debugging support.

Storage Affinity: **RecordStore** (operation log records with timestamps,
                  agent IDs, zone scoping, and metadata snapshots).

References:
    - docs/architecture/ops-scenario-matrix.md  (S24)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OperationLogProtocol(Protocol):
    """Service contract for filesystem operation logging.

    Mirrors ``storage/operation_logger.OperationLogger``.

    Provides audit trail, undo capability (via snapshot_hash),
    and agent activity analysis.
    """

    def log_operation(
        self,
        operation_type: str,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        new_path: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
        status: str = "success",
        error_message: str | None = None,
        flush: bool = True,
    ) -> str:
        """Log a filesystem operation.

        Returns:
            operation_id: UUID of logged operation.
        """
        ...

    def get_operation(self, operation_id: str) -> Any | None:
        """Get operation by ID.

        Returns:
            Operation log entry or None if not found.
        """
        ...

    def list_operations(
        self,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        path: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        path_pattern: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]:
        """List operations with optional filters.

        Returns:
            List of operation log entries, most recent first.
        """
        ...

    def list_operations_cursor(
        self,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        path: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        path_pattern: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Any], str | None]:
        """List operations using cursor-based pagination.

        Returns:
            Tuple of (operations list, next_cursor or None).
        """
        ...

    def count_operations(
        self,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        path: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        path_pattern: str | None = None,
    ) -> int:
        """Count operations matching filters."""
        ...

    def get_last_operation(
        self,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        status: str = "success",
    ) -> Any | None:
        """Get the most recent operation matching filters."""
        ...

    def get_path_history(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        """Get operation history for a specific path."""
        ...

    def agent_activity_summary(
        self,
        *,
        agent_id: str,
        zone_id: str,
        since: datetime | None = None,
        recent_paths_limit: int = 10,
    ) -> dict[str, Any]:
        """Get aggregated activity summary for an agent."""
        ...

    def get_metadata_snapshot(self, operation: Any) -> dict[str, Any] | None:
        """Get metadata snapshot from an operation log entry."""
        ...
