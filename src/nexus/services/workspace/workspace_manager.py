"""Workspace snapshot and versioning manager.

Provides workspace-level version control for time-travel debugging and rollback.
"""

import hashlib
import json
import logging
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.types import OperationContext
from nexus.contracts.workspace_manifest import WorkspaceManifest
from nexus.storage.models import WorkspaceSnapshotModel

from .workspace_permissions import check_workspace_permission

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend
    from nexus.contracts.filesystem import NexusFilesystem
    from nexus.contracts.protocols.rebac import ReBACBrickProtocol
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# §2.5 syscall surface for workspace manifest storage. Path namespace
# replaces the previous backend.write_content / read_content hash-addressed
# access (a kernel-internal HAL pillar — see KERNEL-ARCHITECTURE.md §2.5).
_MANIFEST_PATH_PREFIX = "/__sys__/workspace-history"


def _workspace_id_from_path(workspace_path: str) -> str:
    """Sanitize a workspace path into a single-segment identifier."""
    return workspace_path.strip("/").replace("/", "__") or "root"


def _manifest_path(workspace_path: str, snapshot_id: str) -> str:
    return f"{_MANIFEST_PATH_PREFIX}/{_workspace_id_from_path(workspace_path)}/{snapshot_id}.json"


class WorkspaceManager:
    """Manage workspace snapshots for version control and rollback.

    Provides:
    - Snapshot creation (capture entire workspace state)
    - Snapshot restore (rollback to previous state)
    - Snapshot history (list all snapshots)
    - Snapshot diff (compare two snapshots)

    Design:
    - Snapshots are CAS-backed manifests (JSON files listing path → content_id)
    - Zero storage overhead (content already in CAS)
    - Deduplication (same workspace state = same manifest hash)
    """

    def __init__(
        self,
        nexus_fs: "NexusFilesystem",
        backend: "Backend",
        rebac_manager: "ReBACBrickProtocol | None" = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        record_store: "RecordStoreABC | None" = None,
    ):
        """Initialize workspace manager.

        Args:
            nexus_fs: NexusFS handle — workspace listing reaches MetaStore
                through the §2.2 syscall surface (sys_readdir), not the
                kernel-internal MetaStore directly.
            backend: Backend for storing manifest in CAS
            rebac_manager: ReBAC manager for permission checks (optional)
            zone_id: Default zone ID for operations (optional)
            agent_id: Default agent ID for operations (optional)
            record_store: RecordStoreABC instance providing session_factory
        """
        self._nexus_fs = nexus_fs
        # Direct kernel handle for the remaining sys_stat / sys_unlink /
        # sys_setattr calls in restore() — these are existing syscall users,
        # not the listing migration touched in this commit.
        self._kernel: Any = getattr(nexus_fs, "_kernel", None)
        self.backend = backend
        self.rebac_manager = rebac_manager
        self.zone_id = zone_id
        self.agent_id = agent_id
        if record_store is None:
            raise ValueError("record_store is required — use factory.py for DI wiring")
        self.metadata_session_factory = record_store.session_factory

    def _write_manifest(self, workspace_path: str, snapshot_id: str, manifest_bytes: bytes) -> str:
        """Write a manifest blob to the syscall path namespace.

        Returns the blake3-hex manifest hash kept in the SQL row for
        integrity checking (and for the dual-read fallback during the
        deprecation window).
        """
        sys_ctx = OperationContext(user_id="system", groups=[], is_system=True)
        self._nexus_fs.sys_write(
            _manifest_path(workspace_path, snapshot_id),
            manifest_bytes,
            context=sys_ctx,
        )
        return hashlib.sha256(manifest_bytes).hexdigest()

    def _read_manifest(self, snapshot: WorkspaceSnapshotModel) -> bytes:
        """Read a manifest blob from the syscall path namespace.

        Dual-read window (Issue #4218 follow-up): try the §2.5 path first,
        fall back to the legacy hash-addressed backend.read_content for
        snapshots written before the migration. Removal of the fallback
        is tracked alongside the workspace-history cleanup follow-up.
        """
        sys_ctx = OperationContext(user_id="system", groups=[], is_system=True)
        path = _manifest_path(snapshot.workspace_path, snapshot.snapshot_id)
        try:
            return self._nexus_fs.sys_read(path, context=sys_ctx)
        except (FileNotFoundError, NexusFileNotFoundError):
            logger.warning(
                "workspace manifest missing at %s — falling back to legacy "
                "hash-addressed read for snapshot %s; remove once the "
                "deprecation window closes",
                path,
                snapshot.snapshot_id,
            )
            return self.backend.read_content(snapshot.manifest_hash, context=None)

    def _check_workspace_permission(
        self,
        workspace_path: str,
        permission: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        """Check if user or agent has permission on workspace.

        Delegates to shared utility (Issue #1315 C2-B) for DRY reuse
        by both WorkspaceManager and ContextBranchService.

        Args:
            workspace_path: Path to workspace
            permission: Permission to check (e.g., 'snapshot:create', 'snapshot:list')
            user_id: User ID to check (for user operations)
            agent_id: Agent ID to check (for agent operations)
            zone_id: Zone ID for isolation (uses default if not provided)

        Raises:
            NexusPermissionError: If permission check fails
        """
        check_workspace_permission(
            rebac_manager=self.rebac_manager,
            workspace_path=workspace_path,
            permission=permission,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
            default_agent_id=self.agent_id,
            default_zone_id=self.zone_id,
        )

    def create_snapshot(
        self,
        workspace_path: str,
        description: str | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,  # noqa: ARG002  # kept for API compat
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
            from nexus.kernel_helpers import metastore_list_iter

            files = metastore_list_iter(self._kernel, prefix=workspace_prefix)

            # Collect file metadata for manifest
            file_entries: list[tuple[str, str, int, str | None]] = []

            for file_meta in files:
                # Skip directories (no content) and files without content_id
                if file_meta.mime_type == "directory" or not file_meta.content_id:
                    continue

                # Relative path within workspace
                rel_path = file_meta.path[len(workspace_prefix) :]
                file_entries.append(
                    (rel_path, file_meta.content_id, file_meta.size, file_meta.mime_type)
                )

            # Build manifest (handles sorting and deterministic JSON)
            manifest = WorkspaceManifest.from_file_list(file_entries)
            manifest_bytes = manifest.to_json()

            # Generate snapshot_id up front so the manifest path namespace is
            # populated before the SQL row is inserted (failure here keeps the
            # row out of the table — no orphan reference).
            new_snapshot_id = str(_uuid.uuid4())
            manifest_hash = self._write_manifest(workspace_path, new_snapshot_id, manifest_bytes)

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
                snapshot_id=new_snapshot_id,
                workspace_path=workspace_path,
                snapshot_number=next_snapshot_number,
                manifest_hash=manifest_hash,
                file_count=manifest.file_count,
                total_size_bytes=manifest.total_size,
                description=description,
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

            # Read manifest via the §2.5 syscall surface (dual-read window).
            manifest_bytes = self._read_manifest(snapshot)
            manifest = WorkspaceManifest.from_json(manifest_bytes)

            # Get workspace path and ensure it ends with /
            workspace_prefix = snapshot.workspace_path
            if not workspace_prefix.endswith("/"):
                workspace_prefix += "/"

            # Get current workspace files via the §2.5 syscall surface.
            _sys_ctx = OperationContext(user_id="system", groups=[], is_system=True)
            current_entries = self._nexus_fs.sys_readdir(
                workspace_prefix,
                recursive=True,
                details=True,
                context=_sys_ctx,
            )
            current_paths = {
                str(e["path"])[len(workspace_prefix) :]
                for e in current_entries
                if isinstance(e, dict) and e.get("content_id") and e.get("path")
            }

            # Delete files not in snapshot
            manifest_paths = manifest.paths()
            files_deleted = 0
            for current_path in current_paths:
                if current_path not in manifest_paths and not current_path.endswith("/"):
                    full_path = workspace_prefix + current_path
                    self._kernel.sys_unlink(full_path, context=_sys_ctx)
                    files_deleted += 1

            # Restore files from snapshot
            # Note: Content already exists in CAS, we just need to restore metadata
            files_restored = 0

            from datetime import UTC, datetime

            for rel_path in manifest_paths:
                entry = manifest.get(rel_path)
                assert entry is not None  # paths() guarantees entry exists
                full_path = workspace_prefix + rel_path

                # Check if file exists with same content
                existing = self._kernel.sys_stat(full_path, "root")
                if existing and existing.get("content_id") == entry.content_id:
                    continue  # Already up to date

                # Create metadata entry pointing to existing CAS content
                # No need to read/write content - it's already in CAS!
                now_ms = int(datetime.now(UTC).timestamp() * 1000)
                self._kernel.sys_setattr(
                    full_path,
                    entry_type=0,  # DT_REG upsert
                    content_id=entry.content_id,
                    size=entry.size,
                    mime_type=entry.mime_type,
                    version=1,
                    zone_id="root",
                    modified_at_ms=now_ms,
                )
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

            # Read manifests via the §2.5 syscall surface (dual-read window).
            manifest1 = WorkspaceManifest.from_json(self._read_manifest(snap1))
            manifest2 = WorkspaceManifest.from_json(self._read_manifest(snap2))

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
                if entry1.content_id != entry2.content_id:
                    modified.append(
                        {
                            "path": path,
                            "old_size": entry1.size,
                            "new_size": entry2.size,
                            "old_hash": entry1.content_id,
                            "new_hash": entry2.content_id,
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
