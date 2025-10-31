"""Workspace snapshot and versioning manager.

Provides workspace-level version control for time-travel debugging and rollback.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from nexus.core.exceptions import NexusFileNotFoundError, NexusPermissionError
from nexus.storage.models import WorkspaceSnapshotModel

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.rebac_manager import ReBACManager
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore

logger = logging.getLogger(__name__)


class WorkspaceManager:
    """Manage workspace snapshots for version control and rollback.

    Provides:
    - Snapshot creation (capture entire workspace state)
    - Snapshot restore (rollback to previous state)
    - Snapshot history (list all snapshots)
    - Snapshot diff (compare two snapshots)

    Design:
    - Snapshots are CAS-backed manifests (JSON files listing path → content_hash)
    - Zero storage overhead (content already in CAS)
    - Deduplication (same workspace state = same manifest hash)
    """

    def __init__(
        self,
        metadata: SQLAlchemyMetadataStore,
        backend: Backend,
        rebac_manager: ReBACManager | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
    ):
        """Initialize workspace manager.

        Args:
            metadata: Metadata store for querying file information
            backend: Backend for storing manifest in CAS
            rebac_manager: ReBAC manager for permission checks (optional)
            tenant_id: Default tenant ID for operations (optional)
            agent_id: Default agent ID for operations (optional)
        """
        self.metadata = metadata
        self.backend = backend
        self.rebac_manager = rebac_manager
        self.tenant_id = tenant_id
        self.agent_id = agent_id

    def _check_workspace_permission(
        self,
        workspace_path: str,
        permission: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Check if user or agent has permission on workspace.

        Args:
            workspace_path: Path to workspace
            permission: Permission to check (e.g., 'snapshot:create', 'snapshot:list')
            user_id: User ID to check (for user operations)
            agent_id: Agent ID to check (for agent operations)
            tenant_id: Tenant ID for isolation (uses default if not provided)

        Raises:
            NexusPermissionError: If permission check fails

        Note:
            v0.5.0: Now supports both user and agent subjects.
            - If agent_id is provided: subject=("agent", agent_id)
            - Else if user_id is provided: subject=("user", user_id)
            - Else: deny by default (no identity)

            Permission mapping to file operations:
            - snapshot:create, snapshot:restore -> write (modify state)
            - snapshot:list, snapshot:diff -> read (read-only)
        """
        if not self.rebac_manager:
            # No ReBAC manager configured - allow operation
            # This maintains backward compatibility for deployments without ReBAC
            logger.warning(
                f"WorkspaceManager: No ReBAC manager configured, allowing {permission} on {workspace_path}"
            )
            return

        # Use provided IDs or fall back to defaults
        check_agent_id = agent_id or self.agent_id
        check_tenant_id = tenant_id or self.tenant_id

        # Determine subject based on available context
        # v0.5.0: Support both users and agents
        if check_agent_id:
            subject = ("agent", check_agent_id)
            subject_desc = f"agent={check_agent_id}"
        elif user_id:
            subject = ("user", user_id)
            subject_desc = f"user={user_id}"
        else:
            # No identity available - deny by default for security
            logger.error(
                f"WorkspaceManager: No user_id or agent_id provided for permission check: {permission} on {workspace_path}"
            )
            raise NexusPermissionError(
                f"{permission} on workspace {workspace_path} (no user_id or agent_id provided)"
            )

        # Map workspace permissions to file permissions
        # Workspaces are just directories, so we use the existing "file" namespace
        # which already has proper permission mappings (owner/editor/viewer)
        if permission in ("snapshot:create", "snapshot:restore"):
            # Write operations require write permission
            file_permission = "write"
        elif permission in ("snapshot:list", "snapshot:diff"):
            # Read-only operations require read permission
            file_permission = "read"
        else:
            # Unknown permission - default to write for safety
            logger.warning(f"Unknown workspace permission: {permission}, defaulting to write")
            file_permission = "write"

        # Check permission via ReBAC on the FILE object
        has_permission = self.rebac_manager.rebac_check(
            subject=subject,
            permission=file_permission,
            object=("file", workspace_path),
            tenant_id=check_tenant_id,
        )

        if not has_permission:
            logger.warning(
                f"WorkspaceManager: Permission denied for {subject_desc}, "
                f"permission={permission} (mapped to {file_permission}), workspace={workspace_path}, tenant={check_tenant_id}"
            )
            raise NexusPermissionError(
                f"Permission denied: {permission} on workspace {workspace_path}"
            )

    def create_snapshot(
        self,
        workspace_path: str,
        description: str | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace (e.g., "/my-workspace")
            description: Human-readable description of snapshot
            tags: List of tags for categorization
            created_by: User/agent who created the snapshot
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            tenant_id: Tenant ID for isolation (uses default if not provided)

        Returns:
            Snapshot metadata dict with keys:
                - snapshot_id: Unique snapshot identifier
                - snapshot_number: Sequential version number
                - manifest_hash: Hash of snapshot manifest
                - file_count: Number of files in snapshot
                - total_size_bytes: Total size of all files
                - created_at: Snapshot creation timestamp

        Raises:
            NexusPermissionError: If user/agent lacks snapshot:create permission
            BackendError: If manifest cannot be stored
        """
        # Check permission first (v0.5.0: supports both user and agent)
        self._check_workspace_permission(
            workspace_path=workspace_path,
            permission="snapshot:create",
            user_id=user_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

        # Ensure workspace_path ends with / for prefix matching
        workspace_prefix = workspace_path if workspace_path.endswith("/") else workspace_path + "/"

        # Get all files in workspace
        with self.metadata.SessionLocal() as session:
            files = self.metadata.list(prefix=workspace_prefix)

            # PERFORMANCE OPTIMIZATION: Stream manifest to avoid building large dict in memory
            # For large workspaces (10K+ files), building entire manifest in memory can use 100s of MB
            # Instead, we incrementally build JSON and stream to backend

            # First pass: collect metadata and count
            file_entries = []
            total_size = 0
            file_count = 0

            for file_meta in files:
                # Skip directories (no content) and files without etag
                if file_meta.mime_type == "directory" or not file_meta.etag:
                    continue

                # Relative path within workspace
                rel_path = file_meta.path[len(workspace_prefix) :]
                file_entries.append((rel_path, file_meta.etag, file_meta.size, file_meta.mime_type))
                total_size += file_meta.size
                file_count += 1

            # Sort entries by path for deterministic manifest hashing
            file_entries.sort(key=lambda x: x[0])

            # Stream manifest JSON construction
            # Build JSON incrementally to avoid large string in memory
            import io

            manifest_buffer = io.BytesIO()
            manifest_buffer.write(b"{\n")

            for i, (rel_path, etag, size, mime_type) in enumerate(file_entries):
                # Write each entry
                if i > 0:
                    manifest_buffer.write(b",\n")

                # Use json.dumps for individual values to handle escaping correctly
                # This is much lighter than json.dumps on entire dict
                path_json = json.dumps(rel_path)
                etag_json = json.dumps(etag)
                mime_json = json.dumps(mime_type) if mime_type else "null"

                entry = f'  {path_json}: {{"hash": {etag_json}, "size": {size}, "mime_type": {mime_json}}}'
                manifest_buffer.write(entry.encode("utf-8"))

            manifest_buffer.write(b"\n}")
            manifest_bytes = manifest_buffer.getvalue()

            # Store manifest in CAS
            manifest_hash = self.backend.write_content(manifest_bytes, context=None)

            # Get next snapshot number for this workspace
            stmt = (
                select(WorkspaceSnapshotModel.snapshot_number)
                .where(
                    WorkspaceSnapshotModel.workspace_path == workspace_path,
                )
                .order_by(desc(WorkspaceSnapshotModel.snapshot_number))
                .limit(1)
            )
            result = session.execute(stmt).scalar()
            next_snapshot_number = (result or 0) + 1

            # Create snapshot record
            snapshot = WorkspaceSnapshotModel(
                workspace_path=workspace_path,
                snapshot_number=next_snapshot_number,
                manifest_hash=manifest_hash,
                file_count=file_count,
                total_size_bytes=total_size,
                description=description,
                created_by=created_by,
                tags=json.dumps(tags) if tags else None,
            )

            session.add(snapshot)
            session.commit()
            session.refresh(snapshot)

            return {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_number": snapshot.snapshot_number,
                "manifest_hash": snapshot.manifest_hash,
                "file_count": snapshot.file_count,
                "total_size_bytes": snapshot.total_size_bytes,
                "description": snapshot.description,
                "created_by": snapshot.created_by,
                "tags": json.loads(snapshot.tags) if snapshot.tags else [],
                "created_at": snapshot.created_at,
            }

    def restore_snapshot(
        self,
        snapshot_id: str | None = None,
        snapshot_number: int | None = None,
        workspace_path: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_id: Snapshot ID to restore (takes precedence)
            snapshot_number: Snapshot version number to restore
            workspace_path: Workspace path (required if using snapshot_number)
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            tenant_id: Tenant ID for isolation (uses default if not provided)

        Returns:
            Restore operation result with keys:
                - files_restored: Number of files restored
                - files_deleted: Number of current files deleted
                - snapshot_info: Restored snapshot metadata

        Raises:
            ValueError: If neither snapshot_id nor (snapshot_number + workspace_path) provided
            NexusPermissionError: If user/agent lacks snapshot:restore permission
            NexusFileNotFoundError: If snapshot not found
            BackendError: If manifest cannot be read
        """
        with self.metadata.SessionLocal() as session:
            # Find snapshot first to get workspace_path
            if snapshot_id:
                snapshot = session.get(WorkspaceSnapshotModel, snapshot_id)
            elif snapshot_number is not None and workspace_path:
                stmt = select(WorkspaceSnapshotModel).where(
                    WorkspaceSnapshotModel.workspace_path == workspace_path,
                    WorkspaceSnapshotModel.snapshot_number == snapshot_number,
                )
                snapshot = session.execute(stmt).scalar_one_or_none()
            else:
                raise ValueError("Must provide snapshot_id or (snapshot_number + workspace_path)")

            if not snapshot:
                raise NexusFileNotFoundError(
                    path=f"snapshot:{snapshot_id or snapshot_number}",
                    message="Snapshot not found",
                )

            # Check permission to restore this workspace (v0.5.0: supports user_id)
            self._check_workspace_permission(
                workspace_path=snapshot.workspace_path,
                permission="snapshot:restore",
                user_id=user_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
            )

            # Read manifest from CAS
            manifest_bytes = self.backend.read_content(snapshot.manifest_hash, context=None)
            manifest = json.loads(manifest_bytes.decode("utf-8"))

            # Get workspace path and ensure it ends with /
            workspace_prefix = snapshot.workspace_path
            if not workspace_prefix.endswith("/"):
                workspace_prefix += "/"

            # Get current workspace files
            current_files = self.metadata.list(prefix=workspace_prefix)
            current_paths = {
                f.path[len(workspace_prefix) :]
                for f in current_files
                if f.etag  # Only files with content
            }

            # Delete files not in snapshot
            files_deleted = 0
            for current_path in current_paths:
                if current_path not in manifest and not current_path.endswith("/"):
                    full_path = workspace_prefix + current_path
                    self.metadata.delete(full_path)
                    files_deleted += 1

            # Restore files from snapshot
            # Note: Content already exists in CAS, we just need to restore metadata
            files_restored = 0

            from datetime import UTC, datetime

            from nexus.core.metadata import FileMetadata

            for rel_path, file_info in manifest.items():
                full_path = workspace_prefix + rel_path
                content_hash = file_info["hash"]

                # Check if file exists with same content
                existing = self.metadata.get(full_path)
                if existing and existing.etag == content_hash:
                    continue  # Already up to date

                # Create metadata entry pointing to existing CAS content
                # No need to read/write content - it's already in CAS!
                file_meta = FileMetadata(
                    path=full_path,
                    backend_name="local",  # Backend name for CAS
                    physical_path=content_hash,  # CAS uses hash as physical path
                    size=file_info["size"],
                    etag=content_hash,
                    mime_type=file_info.get("mime_type"),
                    modified_at=datetime.now(UTC),
                    version=1,  # Will be updated by metadata store
                    created_by=self.agent_id,  # Track who restored this version
                )
                self.metadata.put(file_meta)
                files_restored += 1

            return {
                "files_restored": files_restored,
                "files_deleted": files_deleted,
                "snapshot_info": {
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_number": snapshot.snapshot_number,
                    "manifest_hash": snapshot.manifest_hash,
                    "file_count": snapshot.file_count,
                    "total_size_bytes": snapshot.total_size_bytes,
                    "description": snapshot.description,
                    "created_at": snapshot.created_at,
                },
            }

    def list_snapshots(
        self,
        workspace_path: str,
        limit: int = 100,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all snapshots for a workspace.

        Args:
            workspace_path: Path to registered workspace
            limit: Maximum number of snapshots to return
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            tenant_id: Tenant ID for isolation (uses default if not provided)

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            NexusPermissionError: If user/agent lacks snapshot:list permission
        """
        # Check permission first (v0.5.0: supports user_id)
        self._check_workspace_permission(
            workspace_path=workspace_path,
            permission="snapshot:list",
            user_id=user_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
        with self.metadata.SessionLocal() as session:
            stmt = (
                select(WorkspaceSnapshotModel)
                .where(
                    WorkspaceSnapshotModel.workspace_path == workspace_path,
                )
                .order_by(desc(WorkspaceSnapshotModel.created_at))
                .limit(limit)
            )

            snapshots = session.execute(stmt).scalars().all()

            return [
                {
                    "snapshot_id": s.snapshot_id,
                    "snapshot_number": s.snapshot_number,
                    "manifest_hash": s.manifest_hash,
                    "file_count": s.file_count,
                    "total_size_bytes": s.total_size_bytes,
                    "description": s.description,
                    "created_by": s.created_by,
                    "tags": json.loads(s.tags) if s.tags else [],
                    "created_at": s.created_at,
                }
                for s in snapshots
            ]

    def diff_snapshots(
        self,
        snapshot_id_1: str,
        snapshot_id_2: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two snapshots and return diff.

        Args:
            snapshot_id_1: First snapshot ID
            snapshot_id_2: Second snapshot ID
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            tenant_id: Tenant ID for isolation (uses default if not provided)

        Returns:
            Diff dict with keys:
                - added: List of files added in snapshot_2
                - removed: List of files removed in snapshot_2
                - modified: List of files modified between snapshots
                - unchanged: Number of unchanged files

        Raises:
            NexusPermissionError: If user/agent lacks snapshot:diff permission
            NexusFileNotFoundError: If either snapshot not found
        """
        with self.metadata.SessionLocal() as session:
            # Load both snapshots
            snap1 = session.get(WorkspaceSnapshotModel, snapshot_id_1)
            snap2 = session.get(WorkspaceSnapshotModel, snapshot_id_2)

            if not snap1:
                raise NexusFileNotFoundError(
                    path=f"snapshot:{snapshot_id_1}", message="Snapshot 1 not found"
                )
            if not snap2:
                raise NexusFileNotFoundError(
                    path=f"snapshot:{snapshot_id_2}", message="Snapshot 2 not found"
                )

            # Check permission for both workspaces (v0.5.0: supports user_id)
            self._check_workspace_permission(
                workspace_path=snap1.workspace_path,
                permission="snapshot:diff",
                user_id=user_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
            # Only check snap2 if it's a different workspace
            if snap1.workspace_path != snap2.workspace_path:
                self._check_workspace_permission(
                    workspace_path=snap2.workspace_path,
                    permission="snapshot:diff",
                    user_id=user_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                )

            # Read manifests
            manifest1 = json.loads(
                self.backend.read_content(snap1.manifest_hash, context=None).decode("utf-8")
            )
            manifest2 = json.loads(
                self.backend.read_content(snap2.manifest_hash, context=None).decode("utf-8")
            )

            # Compute diff
            paths1 = set(manifest1.keys())
            paths2 = set(manifest2.keys())

            added = []
            for path in paths2 - paths1:
                added.append({"path": path, "size": manifest2[path]["size"]})

            removed = []
            for path in paths1 - paths2:
                removed.append({"path": path, "size": manifest1[path]["size"]})

            modified = []
            for path in paths1 & paths2:
                if manifest1[path]["hash"] != manifest2[path]["hash"]:
                    modified.append(
                        {
                            "path": path,
                            "old_size": manifest1[path]["size"],
                            "new_size": manifest2[path]["size"],
                            "old_hash": manifest1[path]["hash"],
                            "new_hash": manifest2[path]["hash"],
                        }
                    )

            unchanged = len(paths1 & paths2) - len(modified)

            return {
                "snapshot_1": {
                    "snapshot_id": snap1.snapshot_id,
                    "snapshot_number": snap1.snapshot_number,
                    "created_at": snap1.created_at,
                },
                "snapshot_2": {
                    "snapshot_id": snap2.snapshot_id,
                    "snapshot_number": snap2.snapshot_number,
                    "created_at": snap2.created_at,
                },
                "added": added,
                "removed": removed,
                "modified": modified,
                "unchanged": unchanged,
            }
