"""Operations service — session-managed facade (S24: Operations Undo).

Provides audit trail queries and undo orchestration for CLI/API callers.
Wraps ``OperationLogger`` (read queries) and ``OperationUndoService``
(mutation reversal) with automatic session lifecycle management.

CLI/API consumers call this service instead of importing storage-layer
classes directly.

References:
    - docs/architecture/ops-scenario-matrix.md     (S24)
    - storage/operation_logger.py                   (OperationLogger)
    - services/versioning/operation_undo_service.py (OperationUndoService)
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from nexus.services.versioning.operation_undo_service import OperationUndoService


class OperationsService:
    """Session-managed facade for operation log queries and undo.

    Each public method opens and closes its own database session,
    returning plain dicts (never ORM objects).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any],
        undo_service: OperationUndoService | None = None,
    ) -> None:
        """Initialise the operations service.

        Args:
            session_factory: Callable returning a context-managed session.
            undo_service: Pre-constructed undo service (None = undo disabled).
        """
        self._session_factory = session_factory
        self._undo_service = undo_service

    # ------------------------------------------------------------------
    # Read queries
    # ------------------------------------------------------------------

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
    ) -> list[dict[str, Any]]:
        """List operations with optional filters.

        Returns:
            List of operation dicts, most recent first.
        """
        from nexus.storage.operation_logger import OperationLogger

        with self._session_factory() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(
                zone_id=zone_id,
                agent_id=agent_id,
                operation_type=operation_type,
                path=path,
                status=status,
                since=since,
                until=until,
                path_pattern=path_pattern,
                limit=limit,
                offset=offset,
            )
            return [self._op_to_dict(op) for op in ops]

    def get_last_operation(
        self,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        status: str = "success",
    ) -> dict[str, Any] | None:
        """Get the most recent operation matching filters.

        Returns:
            Operation dict or None if no match.
        """
        from nexus.storage.operation_logger import OperationLogger

        with self._session_factory() as session:
            logger = OperationLogger(session)
            op = logger.get_last_operation(
                zone_id=zone_id,
                agent_id=agent_id,
                operation_type=operation_type,
                status=status,
            )
            return self._op_to_dict(op) if op else None

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo_by_id(self, operation_id: str) -> dict[str, Any]:
        """Undo a single operation by its ID.

        Fetches the operation in a fresh session and delegates to
        ``OperationUndoService``.

        Args:
            operation_id: UUID of the operation to undo.

        Returns:
            Dict with keys: success (bool), message (str),
            operation_type (str), path (str).
        """
        if self._undo_service is None:
            return {
                "success": False,
                "message": "Undo service not available",
                "operation_type": "",
                "path": "",
            }

        from nexus.storage.operation_logger import OperationLogger

        with self._session_factory() as session:
            logger = OperationLogger(session)
            op = logger.get_operation(operation_id)

            if not op:
                return {
                    "success": False,
                    "message": f"Operation {operation_id} not found",
                    "operation_type": "",
                    "path": "",
                }

            result = self._undo_service.undo_operation(op)
            return {
                "success": result.success,
                "message": result.message,
                "operation_type": result.operation_type,
                "path": result.path,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _op_to_dict(op: Any) -> dict[str, Any]:
        """Convert ORM operation to a plain dict."""
        return {
            "operation_id": op.operation_id,
            "operation_type": op.operation_type,
            "path": op.path,
            "new_path": op.new_path,
            "agent_id": op.agent_id,
            "zone_id": op.zone_id,
            "status": op.status,
            "created_at": op.created_at,
            "snapshot_hash": op.snapshot_hash,
            "error_message": op.error_message,
        }
