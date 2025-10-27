"""Time-travel debugging functionality for reading files at historical points.

Provides the ability to query filesystem state at any historical operation point,
enabling debugging, analysis, and understanding of agent behavior over time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select

from nexus.core.exceptions import NotFoundError
from nexus.storage.models import FilePathModel, OperationLogModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.backend_base import Backend


class TimeTravelReader:
    """Read filesystem state at historical operation points."""

    def __init__(self, session: Session, backend: Backend):
        """Initialize time-travel reader.

        Args:
            session: SQLAlchemy session for database operations
            backend: Backend for reading content from CAS
        """
        self.session = session
        self.backend = backend

    def get_operation_by_id(self, operation_id: str) -> OperationLogModel:
        """Get operation by ID.

        Args:
            operation_id: Operation UUID

        Returns:
            Operation log entry

        Raises:
            NotFoundError: If operation not found
        """
        stmt = select(OperationLogModel).where(OperationLogModel.operation_id == operation_id)
        operation = self.session.execute(stmt).scalar_one_or_none()

        if not operation:
            raise NotFoundError(f"Operation {operation_id} not found")

        return operation

    def get_file_at_operation(
        self,
        path: str,
        operation_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Get file content and metadata at a specific operation point.

        This reconstructs the file state after the specified operation completed.

        The operation log stores snapshot_hash (previous content) and we need to
        reconstruct what content existed after each operation:
        - For a write at time T, the new content becomes current
        - The next write's snapshot_hash contains the content from time T
        - If no next write, the current metadata has the content

        Args:
            path: File path to query
            operation_id: Operation ID to query state at
            tenant_id: Tenant ID for multi-tenancy

        Returns:
            Dict with keys: content (bytes), metadata (dict), operation_id (str)

        Raises:
            NotFoundError: If file doesn't exist at that operation point
            ValidationError: If operation ID is invalid
        """
        # Get the target operation
        target_op = self.get_operation_by_id(operation_id)

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

        if tenant_id is not None:
            stmt = stmt.where(OperationLogModel.tenant_id == tenant_id)

        all_operations = list(self.session.execute(stmt).scalars())

        # Find operations up to target
        ops_up_to_target = [op for op in all_operations if op.created_at <= target_op.created_at]

        if not ops_up_to_target:
            raise NotFoundError(f"File {path} did not exist at operation {operation_id}")

        # Find most recent operation at or before target
        most_recent = ops_up_to_target[-1]

        # If most recent operation is delete, file doesn't exist
        if most_recent.operation_type == "delete":
            raise NotFoundError(f"File {path} was deleted at or before operation {operation_id}")

        # Find last write operation
        last_write = None
        for op in reversed(ops_up_to_target):
            if op.operation_type == "write":
                last_write = op
                break

        if not last_write:
            raise NotFoundError(f"File {path} had no write operations before {operation_id}")

        # Now reconstruct the content written by last_write
        # Strategy: The next write operation's snapshot_hash contains our content
        ops_after_write = [op for op in all_operations if op.created_at > last_write.created_at]

        next_write = None
        for op in ops_after_write:
            if op.operation_type == "write":
                next_write = op
                break

        content = None
        metadata_dict = {}

        if next_write and next_write.snapshot_hash:
            # The next write's snapshot contains the content from last_write
            content = self.backend.read_content(next_write.snapshot_hash)
            if next_write.metadata_snapshot:
                metadata_dict = json.loads(next_write.metadata_snapshot)
        else:
            # No next write, or next write has no snapshot (new file creation)
            # Check if file still exists in current metadata
            path_stmt = select(FilePathModel).where(FilePathModel.virtual_path == path)
            if tenant_id is not None:
                path_stmt = path_stmt.where(FilePathModel.tenant_id == tenant_id)

            current_path = self.session.execute(path_stmt).scalar_one_or_none()

            if current_path:
                # File still exists with same content from last_write
                content = self.backend.read_content(current_path.content_hash)
                metadata_dict = {
                    "size": current_path.size_bytes,
                    # v0.5.0: owner/group/mode removed - use ReBAC for permissions
                    "version": current_path.current_version,
                    "etag": current_path.content_hash,
                    "modified_at": current_path.updated_at.isoformat()
                    if current_path.updated_at
                    else None,
                }
            else:
                # File was deleted after last_write but we need content at last_write
                # Look for delete operation's snapshot
                next_delete = None
                for op in ops_after_write:
                    if op.operation_type == "delete":
                        next_delete = op
                        break

                if next_delete and next_delete.snapshot_hash:
                    content = self.backend.read_content(next_delete.snapshot_hash)
                    if next_delete.metadata_snapshot:
                        metadata_dict = json.loads(next_delete.metadata_snapshot)
                else:
                    raise NotFoundError(
                        f"Cannot reconstruct content for {path} at operation {operation_id}"
                    )

        if content is None:
            raise NotFoundError(
                f"Cannot reconstruct content for {path} at operation {operation_id}"
            )

        return {
            "content": content,
            "metadata": metadata_dict,
            "operation_id": last_write.operation_id,
            "operation_time": last_write.created_at.isoformat(),
        }

    def list_files_at_operation(
        self,
        directory: str,
        operation_id: str,
        *,
        tenant_id: str | None = None,
        recursive: bool = False,
    ) -> list[dict[str, Any]]:
        """List files in a directory at a specific operation point.

        Args:
            directory: Directory path to list
            operation_id: Operation ID to query state at
            tenant_id: Tenant ID for multi-tenancy
            recursive: Whether to list recursively

        Returns:
            List of dicts with keys: path, size, modified_at

        Raises:
            NotFoundError: If directory doesn't exist at that operation point
            ValidationError: If operation ID is invalid
        """
        # Get the target operation
        target_op = self.get_operation_by_id(operation_id)

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
            # Non-recursive: only direct children
            # This is approximate - we'll filter more precisely later
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

        if tenant_id is not None:
            stmt = stmt.where(OperationLogModel.tenant_id == tenant_id)

        operations = list(self.session.execute(stmt).scalars())

        # Group operations by path and find latest state for each
        file_states: dict[str, OperationLogModel] = {}

        for op in operations:
            path = op.path

            # Skip if not in directory (for non-recursive case)
            if not recursive:
                rel_path = path[len(directory) :] if path.startswith(directory) else None
                if not rel_path or "/" in rel_path:
                    continue

            # Track most recent operation per path (operations are ordered by path, then time desc)
            if path not in file_states:
                file_states[path] = op

        # Filter out deleted files and build result
        result: list[dict[str, Any]] = []

        for path, latest_op in file_states.items():
            # If latest operation is delete, file doesn't exist
            if latest_op.operation_type == "delete":
                continue

            # If latest operation is write, file exists
            if latest_op.operation_type == "write":
                metadata = (
                    json.loads(latest_op.metadata_snapshot) if latest_op.metadata_snapshot else {}
                )

                result.append(
                    {
                        "path": path,
                        "size": metadata.get("size", 0),
                        "modified_at": metadata.get("modified_at"),
                        # v0.5.0: owner/group/mode removed - use ReBAC for permissions
                    }
                )

        return sorted(result, key=lambda x: x["path"])

    def diff_operations(
        self,
        path: str,
        operation_id_1: str,
        operation_id_2: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare file state between two operation points.

        Args:
            path: File path to compare
            operation_id_1: First operation ID
            operation_id_2: Second operation ID
            tenant_id: Tenant ID for multi-tenancy

        Returns:
            Dict with keys:
                - operation_1: First operation state (or None if not exists)
                - operation_2: Second operation state (or None if not exists)
                - content_changed: Boolean indicating if content differs
                - size_diff: Size difference in bytes

        Raises:
            ValidationError: If operation IDs are invalid
        """
        from contextlib import suppress

        # Get states at both operations
        state_1 = None
        state_2 = None

        with suppress(NotFoundError):
            state_1 = self.get_file_at_operation(path, operation_id_1, tenant_id=tenant_id)

        with suppress(NotFoundError):
            state_2 = self.get_file_at_operation(path, operation_id_2, tenant_id=tenant_id)

        # Compare states
        content_changed = True
        size_diff = 0

        if state_1 and state_2:
            content_changed = state_1["content"] != state_2["content"]
            size_diff = state_2["metadata"]["size"] - state_1["metadata"]["size"]
        elif state_1 and not state_2:
            # File was deleted
            size_diff = -state_1["metadata"]["size"]
        elif not state_1 and state_2:
            # File was created
            size_diff = state_2["metadata"]["size"]
        else:
            # File doesn't exist at either point
            content_changed = False

        return {
            "operation_1": state_1,
            "operation_2": state_2,
            "content_changed": content_changed,
            "size_diff": size_diff,
        }
