"""Time-travel debugging protocol (ops-scenario-matrix S24 extension).

Defines the contract for read-side operation-point queries — retrieving
filesystem state at any historical operation point for debugging and analysis.

Complements OperationLogProtocol (write-side audit trail) and
OperationUndoService (mutation reversal).

Storage Affinity: **RecordStore** (operation log) + **ObjectStore** (CAS content).

References:
    - docs/architecture/ops-scenario-matrix.md  (S24)
    - services/versioning/time_travel_service.py (concrete implementation)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TimeTravelProtocol(Protocol):
    """Service contract for read-side time-travel debugging.

    All methods are session-managed: callers never need to handle
    SQLAlchemy sessions or construct storage-layer objects.
    """

    def get_file_at_operation(
        self,
        path: str,
        operation_id: str,
        *,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Get file content and metadata at a specific operation point.

        Args:
            path: File path to query.
            operation_id: Operation ID to query state at.
            zone_id: Zone ID for multi-tenancy (None = default zone).

        Returns:
            Dict with keys: content (bytes), metadata (dict),
            operation_id (str), operation_time (str).

        Raises:
            NexusFileNotFoundError: If file doesn't exist at that point.
        """
        ...

    def list_files_at_operation(
        self,
        directory: str,
        operation_id: str,
        *,
        zone_id: str | None = None,
        recursive: bool = False,
    ) -> list[dict[str, Any]]:
        """List files in a directory at a specific operation point.

        Args:
            directory: Directory path to list.
            operation_id: Operation ID to query state at.
            zone_id: Zone ID for multi-tenancy (None = default zone).
            recursive: Whether to list recursively.

        Returns:
            List of dicts with keys: path, size, modified_at.
        """
        ...

    def diff_operations(
        self,
        path: str,
        operation_id_1: str,
        operation_id_2: str,
        *,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare file state between two operation points.

        Args:
            path: File path to compare.
            operation_id_1: First operation ID.
            operation_id_2: Second operation ID.
            zone_id: Zone ID for multi-tenancy (None = default zone).

        Returns:
            Dict with keys: operation_1, operation_2 (state dicts or None),
            content_changed (bool), size_diff (int).
        """
        ...
