"""Workspace snapshot and versioning manager.

Provides workspace-level version control for time-travel debugging and rollback.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from nexus.core.exceptions import NexusFileNotFoundError, NexusPermissionError
from nexus.core.workspace_manifest import WorkspaceManifest
from nexus.storage.models import WorkspaceSnapshotModel

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core._metadata_generated import FileMetadataProtocol
    from nexus.core.rebac_manager import ReBACManager

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
        metadata: FileMetadataProtocol,
        backend: Backend,
        rebac_manager: ReBACManager | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        session_factory: Any | None = None,
    ):
        """Initialize workspace manager.

        Args:
            metadata: Metadata store for querying file information
            backend: Backend for storing manifest in CAS
            rebac_manager: ReBAC manager for permission checks (optional)
            zone_id: Default zone ID for operations (optional)
            agent_id: Default agent ID for operations (optional)
            session_factory: SQLAlchemy session factory for database operations
        """
        self.metadata = metadata
        self.backend = backend
        self.rebac_manager = rebac_manager
        self.zone_id = zone_id
        self.agent_id = agent_id
        # Use provided session_factory or import from database module
        if session_factory is not None:
            self.metadata_session_factory = session_factory
        else:
            from nexus.storage.database import get_session_factory

            self.metadata_session_factory = get_session_factory()

    def _check_workspace_permission(
        self,
        workspace_path: str,
        permission: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        """Check if user or agent has permission on workspace.

        Args:
            workspace_path: Path to workspace
            permission: Permission to check (e.g., 'snapshot:create', 'snapshot:list')
            user_id: User ID to check (for user operations)
            agent_id: Agent ID to check (for agent operations)
            zone_id: Zone ID for isolation (uses default if not provided)

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
        check_zone_id = zone_id or self.zone_id

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
            zone_id=check_zone_id,
        )

        if not has_permission:
            logger.warning(
                f"WorkspaceManager: Permission denied for {subject_desc}, "
                f"permission={permission} (mapped to {file_permission}), workspace={workspace_path}, zone={check_zone_id}"
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
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace (e.g., "/my-workspace")
            description: Human-readable description of snapshot
            tags: List of tags for categorization
            created_by: User/agent who created the snapshot
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            zone_id: Zone ID for isolation (uses default if not provided)

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
            zone_id=zone_id,
        )

        # Ensure workspace_path ends with / for prefix matching
        workspace_prefix = workspace_path if workspace_path.endswith("/") else workspace_path + "/"

        # Get all files in workspace
        with self.metadata_session_factory() as session:
            files = self.metadata.list(prefix=workspace_prefix)

            # Collect file metadata for manifest
            file_entries: list[tuple[str, str, int, str | None]] = []

            for file_meta in files:
                # Skip directories (no content) and files without etag
                if file_meta.mime_type == "directory" or not file_meta.etag:
                    continue

                # Relative path within workspace
                rel_path = file_meta.path[len(workspace_prefix) :]
                file_entries.append((rel_path, file_meta.etag, file_meta.size, file_meta.mime_type))

            # Build manifest (handles sorting and deterministic JSON)
            manifest = WorkspaceManifest.from_file_list(file_entries)
            manifest_bytes = manifest.to_json()

            # Store manifest in CAS
            manifest_hash = self.backend.write_content(manifest_bytes, context=None).unwrap()

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
                file_count=manifest.file_count,
                total_size_bytes=manifest.total_size,
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
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_id: Snapshot ID to restore (takes precedence)
            snapshot_number: Snapshot version number to restore
            workspace_path: Workspace path (required if using snapshot_number)
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            zone_id: Zone ID for isolation (uses default if not provided)

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
        with self.metadata_session_factory() as session:
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
                zone_id=zone_id,
            )

            # Read manifest from CAS
            manifest_bytes = self.backend.read_content(
                snapshot.manifest_hash, context=None
            ).unwrap()
            manifest = WorkspaceManifest.from_json(manifest_bytes)

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
            manifest_paths = manifest.paths()
            files_deleted = 0
            for current_path in current_paths:
                if current_path not in manifest_paths and not current_path.endswith("/"):
                    full_path = workspace_prefix + current_path
                    self.metadata.delete(full_path)
                    files_deleted += 1

            # Restore files from snapshot
            # Note: Content already exists in CAS, we just need to restore metadata
            files_restored = 0

            from datetime import UTC, datetime

            from nexus.core._metadata_generated import FileMetadata

            for rel_path in manifest_paths:
                entry = manifest.get(rel_path)
                assert entry is not None  # paths() guarantees entry exists
                full_path = workspace_prefix + rel_path

                # Check if file exists with same content
                existing = self.metadata.get(full_path)
                if existing and existing.etag == entry.content_hash:
                    continue  # Already up to date

                # Create metadata entry pointing to existing CAS content
                # No need to read/write content - it's already in CAS!
                file_meta = FileMetadata(
                    path=full_path,
                    backend_name="local",  # Backend name for CAS
                    physical_path=entry.content_hash,  # CAS uses hash as physical path
                    size=entry.size,
                    etag=entry.content_hash,
                    mime_type=entry.mime_type,
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
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all snapshots for a workspace.

        Args:
            workspace_path: Path to registered workspace
            limit: Maximum number of snapshots to return
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            zone_id: Zone ID for isolation (uses default if not provided)

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
            zone_id=zone_id,
        )
        with self.metadata_session_factory() as session:
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
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two snapshots and return diff.

        Args:
            snapshot_id_1: First snapshot ID
            snapshot_id_2: Second snapshot ID
            user_id: User ID for permission check (v0.5.0)
            agent_id: Agent ID for permission check (uses default if not provided)
            zone_id: Zone ID for isolation (uses default if not provided)

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
        with self.metadata_session_factory() as session:
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
                zone_id=zone_id,
            )
            # Only check snap2 if it's a different workspace
            if snap1.workspace_path != snap2.workspace_path:
                self._check_workspace_permission(
                    workspace_path=snap2.workspace_path,
                    permission="snapshot:diff",
                    user_id=user_id,
                    agent_id=agent_id,
                    zone_id=zone_id,
                )

            # Read manifests
            manifest1 = WorkspaceManifest.from_json(
                self.backend.read_content(snap1.manifest_hash, context=None).unwrap()
            )
            manifest2 = WorkspaceManifest.from_json(
                self.backend.read_content(snap2.manifest_hash, context=None).unwrap()
            )

            # Compute diff
            paths1 = manifest1.paths()
            paths2 = manifest2.paths()

            added = []
            for path in paths2 - paths1:
                entry = manifest2.get(path)
                assert entry is not None
                added.append({"path": path, "size": entry.size})

            removed = []
            for path in paths1 - paths2:
                entry = manifest1.get(path)
                assert entry is not None
                removed.append({"path": path, "size": entry.size})

            modified = []
            for path in paths1 & paths2:
                entry1 = manifest1.get(path)
                entry2 = manifest2.get(path)
                assert entry1 is not None and entry2 is not None
                if entry1.content_hash != entry2.content_hash:
                    modified.append(
                        {
                            "path": path,
                            "old_size": entry1.size,
                            "new_size": entry2.size,
                            "old_hash": entry1.content_hash,
                            "new_hash": entry2.content_hash,
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

    # === Issue #1264: Overlay operations ===

    def flatten_overlay(
        self,
        workspace_path: str,
        overlay_resolver: Any,
        overlay_config: Any,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Flatten an overlay workspace into a new snapshot.

        Merges the upper layer (modifications) into a new immutable manifest,
        stores it in CAS, and records a new snapshot.

        Args:
            workspace_path: Path to the overlay workspace
            overlay_resolver: OverlayResolver service instance
            overlay_config: OverlayConfig for this workspace
            user_id: User ID for permission check
            agent_id: Agent ID for permission check
            zone_id: Zone ID for isolation

        Returns:
            New snapshot metadata dict

        Raises:
            NexusPermissionError: If user/agent lacks snapshot:create permission
            ValueError: If overlay is not enabled
        """
        self._check_workspace_permission(
            workspace_path=workspace_path,
            permission="snapshot:create",
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

        # Flatten overlay into new manifest
        merged_manifest = overlay_resolver.flatten(overlay_config)
        manifest_bytes = merged_manifest.to_json()

        # Store flattened manifest in CAS
        manifest_hash = self.backend.write_content(manifest_bytes, context=None).unwrap()

        return {
            "manifest_hash": manifest_hash,
            "file_count": merged_manifest.file_count,
            "total_size_bytes": merged_manifest.total_size,
        }

    def overlay_stats(
        self,
        workspace_path: str,
        overlay_resolver: Any,
        overlay_config: Any,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Get storage statistics for an overlay workspace.

        Args:
            workspace_path: Path to the overlay workspace
            overlay_resolver: OverlayResolver service instance
            overlay_config: OverlayConfig for this workspace
            user_id: User ID for permission check
            agent_id: Agent ID for permission check
            zone_id: Zone ID for isolation

        Returns:
            Storage statistics dict with shared_ratio, savings, etc.

        Raises:
            NexusPermissionError: If user/agent lacks snapshot:list permission
        """
        self._check_workspace_permission(
            workspace_path=workspace_path,
            permission="snapshot:list",
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

        stats = overlay_resolver.overlay_stats(overlay_config)
        return stats.to_dict()
