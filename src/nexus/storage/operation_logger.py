"""Operation logger for tracking filesystem operations.

Provides audit trail, undo capability, and debugging support.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from nexus.storage.models import OperationLogModel


class OperationLogger:
    """Logs filesystem operations for audit, undo, and debugging."""

    def __init__(self, session: Session):
        """Initialize operation logger.

        Args:
            session: SQLAlchemy session for database operations.
        """
        self.session = session

    def log_operation(
        self,
        operation_type: str,
        path: str,
        *,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        new_path: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> str:
        """Log a filesystem operation.

        Args:
            operation_type: Type of operation (write, delete, rename, etc.)
            path: Path affected by operation
            tenant_id: Tenant ID for multi-tenancy
            agent_id: Agent ID performing operation
            new_path: New path for rename operations
            snapshot_hash: CAS hash of previous content
            metadata_snapshot: Previous file metadata (owner, group, mode, etc.)
            status: Operation status (success/failure)
            error_message: Error message if operation failed

        Returns:
            operation_id: UUID of logged operation
        """
        operation = OperationLogModel(
            operation_type=operation_type,
            path=path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            new_path=new_path,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=json.dumps(metadata_snapshot) if metadata_snapshot else None,
            status=status,
            error_message=error_message,
            created_at=datetime.now(UTC),
        )

        operation.validate()
        self.session.add(operation)
        self.session.flush()  # Get the operation_id

        return operation.operation_id

    def get_operation(self, operation_id: str) -> OperationLogModel | None:
        """Get operation by ID.

        Args:
            operation_id: Operation UUID

        Returns:
            Operation log entry or None if not found
        """
        stmt = select(OperationLogModel).where(OperationLogModel.operation_id == operation_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_operations(
        self,
        *,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        path: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OperationLogModel]:
        """List operations with optional filters.

        Args:
            tenant_id: Filter by tenant ID
            agent_id: Filter by agent ID
            operation_type: Filter by operation type
            path: Filter by path (exact match)
            status: Filter by status (success/failure)
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of operation log entries, most recent first
        """
        stmt = select(OperationLogModel).order_by(desc(OperationLogModel.created_at))

        # Apply filters
        if tenant_id is not None:
            stmt = stmt.where(OperationLogModel.tenant_id == tenant_id)
        if agent_id is not None:
            stmt = stmt.where(OperationLogModel.agent_id == agent_id)
        if operation_type is not None:
            stmt = stmt.where(OperationLogModel.operation_type == operation_type)
        if path is not None:
            stmt = stmt.where(OperationLogModel.path == path)
        if status is not None:
            stmt = stmt.where(OperationLogModel.status == status)

        stmt = stmt.limit(limit).offset(offset)

        return list(self.session.execute(stmt).scalars())

    def get_last_operation(
        self,
        *,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        status: str = "success",
    ) -> OperationLogModel | None:
        """Get the last successful operation.

        Args:
            tenant_id: Filter by tenant ID
            agent_id: Filter by agent ID
            operation_type: Filter by operation type
            status: Filter by status (default: success)

        Returns:
            Most recent operation matching filters or None
        """
        operations = self.list_operations(
            tenant_id=tenant_id,
            agent_id=agent_id,
            operation_type=operation_type,
            status=status,
            limit=1,
        )
        return operations[0] if operations else None

    def get_path_history(
        self,
        path: str,
        *,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[OperationLogModel]:
        """Get operation history for a specific path.

        Args:
            path: Virtual path
            tenant_id: Filter by tenant ID
            limit: Maximum number of results

        Returns:
            List of operations affecting this path, most recent first
        """
        return self.list_operations(
            path=path,
            tenant_id=tenant_id,
            limit=limit,
        )

    def get_metadata_snapshot(self, operation: OperationLogModel) -> dict[str, Any] | None:
        """Get metadata snapshot from operation.

        Args:
            operation: Operation log entry

        Returns:
            Metadata snapshot as dict or None
        """
        if operation.metadata_snapshot:
            parsed: dict[str, Any] = json.loads(operation.metadata_snapshot)
            return parsed
        return None
