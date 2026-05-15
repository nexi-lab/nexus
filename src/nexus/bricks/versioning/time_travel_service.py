"""Time-travel debugging service (S24 extension).

Session-managed service providing read-side operation-point queries —
retrieving filesystem state at any historical operation point for
debugging and analysis.

Merges ``storage/time_travel.TimeTravelReader`` logic into a proper
service-layer object with automatic session lifecycle management.
The storage-layer class (``TimeTravelReader``) is deleted; this service
is the sole implementation.

Storage Affinity: **RecordStore** (operation log) + **ObjectStore** (CAS content).

References:
    - docs/architecture/ops-scenario-matrix.md  (S24)
    - services/protocols/time_travel.py          (TimeTravelProtocol)
"""

import json
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from nexus.backends.base.backend import Backend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.storage.models import FilePathModel, OperationLogModel


class TimeTravelService:
    """Session-managed time-travel debugging service.

    Each public method opens and closes its own database session,
    returning plain dicts (never ORM objects).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any],
        backend: Backend | None = None,
        default_zone_id: str | None = None,
    ) -> None:
        """Initialise the time-travel service.

        Args:
            session_factory: Callable returning a context-managed session.
            backend: Backend for reading content from CAS.
            default_zone_id: Zone ID to use when callers omit zone_id.
        """
        self._session_factory = session_factory
        self._backend = backend
        self._default_zone_id = default_zone_id

    # ------------------------------------------------------------------
    # Public API (matches TimeTravelProtocol)
    # ------------------------------------------------------------------

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
            zone_id: Zone ID for multi-tenancy (None = use default zone).

        Returns:
            Dict with keys: content (bytes), metadata (dict),
            operation_id (str), operation_time (str).

        Raises:
            NexusFileNotFoundError: If file doesn't exist at that point.
        """
        effective_zone_id = zone_id if zone_id is not None else self._default_zone_id
        with self._session_factory() as session:
            return self._get_file_at_operation(session, path, operation_id, effective_zone_id)

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
            zone_id: Zone ID for multi-tenancy (None = use default zone).
            recursive: Whether to list recursively.

        Returns:
            List of dicts with keys: path, size, modified_at.
        """
        effective_zone_id = zone_id if zone_id is not None else self._default_zone_id
        with self._session_factory() as session:
            return self._list_files_at_operation(
                session, directory, operation_id, effective_zone_id, recursive
            )

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
            zone_id: Zone ID for multi-tenancy (None = use default zone).

        Returns:
            Dict with keys: operation_1, operation_2 (state dicts or None),
            content_changed (bool), size_diff (int).
        """
        effective_zone_id = zone_id if zone_id is not None else self._default_zone_id
        with self._session_factory() as session:
            # Get states at both operations
            state_1 = None
            state_2 = None

            with suppress(NexusFileNotFoundError):
                state_1 = self._get_file_at_operation(
                    session, path, operation_id_1, effective_zone_id
                )

            with suppress(NexusFileNotFoundError):
                state_2 = self._get_file_at_operation(
                    session, path, operation_id_2, effective_zone_id
                )

            # Compare states
            content_changed = True
            size_diff = 0

            if state_1 and state_2:
                content_changed = state_1["content"] != state_2["content"]
                size_diff = self._content_size(state_2) - self._content_size(state_1)
            elif state_1 and not state_2:
                size_diff = -self._content_size(state_1)
            elif not state_1 and state_2:
                size_diff = self._content_size(state_2)
            else:
                content_changed = False

            return {
                "operation_1": state_1,
                "operation_2": state_2,
                "content_changed": content_changed,
                "size_diff": size_diff,
            }

    @staticmethod
    def _content_size(state: dict[str, Any]) -> int:
        content = state.get("content", b"")
        if isinstance(content, bytes):
            return len(content)
        if isinstance(content, str):
            return len(content.encode())
        return len(bytes(content))

    # ------------------------------------------------------------------
    # Internal helpers (merged from storage/time_travel.TimeTravelReader)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_operation_by_id(session: Session, operation_id: str) -> OperationLogModel:
        """Get operation by ID.

        Raises:
            NexusFileNotFoundError: If operation not found.
        """
        stmt = select(OperationLogModel).where(OperationLogModel.operation_id == operation_id)
        operation = session.execute(stmt).scalar_one_or_none()

        if not operation:
            raise NexusFileNotFoundError(f"Operation {operation_id} not found")

        return operation

    def _get_file_at_operation(
        self,
        session: Session,
        path: str,
        operation_id: str,
        zone_id: str | None,
    ) -> dict[str, Any]:
        """Core logic for reconstructing file state at an operation point."""
        target_op = self._get_operation_by_id(session, operation_id)

        # Find all successful operations for this path, ordered by time
        stmt = (
            select(OperationLogModel)
            .where(
                and_(
                    OperationLogModel.path == path,
                    OperationLogModel.status == "success",
                )
            )
            .order_by(OperationLogModel.created_at.asc())
        )

        if zone_id is not None:
            stmt = stmt.where(OperationLogModel.zone_id == zone_id)

        all_operations = list(session.execute(stmt).scalars())

        # Find operations up to target
        ops_up_to_target = [op for op in all_operations if op.created_at <= target_op.created_at]

        if not ops_up_to_target:
            raise NexusFileNotFoundError(f"File {path} did not exist at operation {operation_id}")

        # Find most recent operation at or before target
        most_recent = ops_up_to_target[-1]

        if most_recent.operation_type == "delete":
            raise NexusFileNotFoundError(
                f"File {path} was deleted at or before operation {operation_id}"
            )

        # Find last write operation
        last_write = None
        for op in reversed(ops_up_to_target):
            if op.operation_type == "write":
                last_write = op
                break

        if not last_write:
            raise NexusFileNotFoundError(
                f"File {path} had no write operations before {operation_id}"
            )

        # Reconstruct the content written by last_write
        ops_after_write = [op for op in all_operations if op.created_at > last_write.created_at]

        next_write = None
        for op in ops_after_write:
            if op.operation_type == "write":
                next_write = op
                break

        content = None
        metadata_dict: dict[str, Any] = {}

        if next_write and next_write.snapshot_hash and self._backend is not None:
            content = self._backend.read_content(next_write.snapshot_hash, context=None)
            if next_write.metadata_snapshot:
                metadata_dict = json.loads(next_write.metadata_snapshot)
        else:
            path_stmt = select(FilePathModel).where(FilePathModel.virtual_path == path)
            if zone_id is not None:
                path_stmt = path_stmt.where(FilePathModel.zone_id == zone_id)

            current_path = session.execute(path_stmt).scalar_one_or_none()

            if current_path:
                content_id = current_path.content_id
                if content_id is None:
                    raise NexusFileNotFoundError(f"File {path} has no content hash")
                if self._backend is not None:
                    content = self._backend.read_content(content_id, context=None)
                metadata_dict = {
                    "size": current_path.size_bytes,
                    "version": current_path.current_version,
                    "content_id": current_path.content_id,
                    "modified_at": current_path.updated_at.isoformat()
                    if current_path.updated_at
                    else None,
                }
            else:
                next_delete = None
                for op in ops_after_write:
                    if op.operation_type == "delete":
                        next_delete = op
                        break

                if next_delete and next_delete.snapshot_hash and self._backend is not None:
                    content = self._backend.read_content(next_delete.snapshot_hash, context=None)
                    if next_delete.metadata_snapshot:
                        metadata_dict = json.loads(next_delete.metadata_snapshot)
                else:
                    raise NexusFileNotFoundError(
                        f"Cannot reconstruct content for {path} at operation {operation_id}"
                    )

        if content is None:
            raise NexusFileNotFoundError(
                f"Cannot reconstruct content for {path} at operation {operation_id}"
            )

        return {
            "content": content,
            "metadata": metadata_dict,
            "operation_id": last_write.operation_id,
            "operation_time": last_write.created_at.isoformat(),
        }

    def _list_files_at_operation(
        self,
        session: Session,
        directory: str,
        operation_id: str,
        zone_id: str | None,
        recursive: bool,
    ) -> list[dict[str, Any]]:
        """Core logic for listing directory at an operation point."""
        target_op = self._get_operation_by_id(session, operation_id)

        # Normalize directory path
        if not directory.endswith("/") and directory != "/":
            directory = directory + "/"

        # Find all operations up to this point that affect files in this directory
        if recursive:
            path_filter = or_(
                OperationLogModel.path.like(f"{directory}%"),
                OperationLogModel.path == directory.rstrip("/"),
            )
        else:
            path_filter = OperationLogModel.path.like(f"{directory}%")

        stmt = (
            select(OperationLogModel)
            .where(
                and_(
                    path_filter,
                    OperationLogModel.created_at <= target_op.created_at,
                    OperationLogModel.status == "success",
                )
            )
            .order_by(OperationLogModel.path, OperationLogModel.created_at.desc())
        )

        if zone_id is not None:
            stmt = stmt.where(OperationLogModel.zone_id == zone_id)

        operations = list(session.execute(stmt).scalars())

        # Group operations by path and find latest state for each
        file_states: dict[str, OperationLogModel] = {}

        for op in operations:
            op_path = op.path

            if not recursive:
                rel_path = op_path[len(directory) :] if op_path.startswith(directory) else None
                if not rel_path or "/" in rel_path:
                    continue

            if op_path not in file_states:
                file_states[op_path] = op

        # Filter out deleted files and build result
        result: list[dict[str, Any]] = []

        for op_path, latest_op in file_states.items():
            if latest_op.operation_type == "delete":
                continue

            if latest_op.operation_type == "write":
                metadata = (
                    json.loads(latest_op.metadata_snapshot) if latest_op.metadata_snapshot else {}
                )

                result.append(
                    {
                        "path": op_path,
                        "size": metadata.get("size", 0),
                        "modified_at": metadata.get("modified_at"),
                    }
                )

        return sorted(result, key=lambda x: x["path"])
