"""Workspace snapshot and versioning manager.

Provides workspace-level version control for time-travel debugging and rollback.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.storage.models import WorkspaceSnapshotModel

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


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

    def __init__(self, metadata: SQLAlchemyMetadataStore, backend: Backend):
        """Initialize workspace manager.

        Args:
            metadata: Metadata store for querying file information
            backend: Backend for storing manifest in CAS
        """
        self.metadata = metadata
        self.backend = backend

    def create_snapshot(
        self,
        agent_id: str,
        tenant_id: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of the agent's workspace.

        Args:
            agent_id: Agent identifier
            tenant_id: Tenant identifier (optional)
            description: Human-readable description of snapshot
            tags: List of tags for categorization
            created_by: User/agent who created the snapshot

        Returns:
            Snapshot metadata dict with keys:
                - snapshot_id: Unique snapshot identifier
                - snapshot_number: Sequential version number
                - manifest_hash: Hash of snapshot manifest
                - file_count: Number of files in snapshot
                - total_size_bytes: Total size of all files
                - created_at: Snapshot creation timestamp

        Raises:
            BackendError: If manifest cannot be stored
        """
        # Build workspace prefix
        workspace_prefix = self._build_workspace_prefix(tenant_id, agent_id)

        # Get all files in workspace
        with self.metadata.SessionLocal() as session:
            files = self.metadata.list(prefix=workspace_prefix)

            # Build manifest: {path: content_hash, ...}
            manifest = {}
            total_size = 0
            for file_meta in files:
                # Skip directories (no content) and files without etag
                if file_meta.mime_type == "directory" or not file_meta.etag:
                    continue

                # Relative path within workspace
                rel_path = file_meta.path[len(workspace_prefix) :]
                manifest[rel_path] = {
                    "hash": file_meta.etag,
                    "size": file_meta.size,
                    "mime_type": file_meta.mime_type,
                }
                total_size += file_meta.size

            # Serialize manifest to JSON
            manifest_json = json.dumps(manifest, sort_keys=True, indent=2)
            manifest_bytes = manifest_json.encode("utf-8")

            # Store manifest in CAS
            manifest_hash = self.backend.write_content(manifest_bytes)

            # Get next snapshot number
            stmt = (
                select(WorkspaceSnapshotModel.snapshot_number)
                .where(
                    WorkspaceSnapshotModel.tenant_id == tenant_id,
                    WorkspaceSnapshotModel.agent_id == agent_id,
                )
                .order_by(desc(WorkspaceSnapshotModel.snapshot_number))
                .limit(1)
            )
            result = session.execute(stmt).scalar()
            next_snapshot_number = (result or 0) + 1

            # Create snapshot record
            snapshot = WorkspaceSnapshotModel(
                tenant_id=tenant_id,
                agent_id=agent_id,
                snapshot_number=next_snapshot_number,
                manifest_hash=manifest_hash,
                file_count=len(manifest),
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
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_id: Snapshot ID to restore (takes precedence)
            snapshot_number: Snapshot version number to restore
            agent_id: Agent identifier (required if using snapshot_number)
            tenant_id: Tenant identifier (optional)

        Returns:
            Restore operation result with keys:
                - files_restored: Number of files restored
                - files_deleted: Number of current files deleted
                - snapshot_info: Restored snapshot metadata

        Raises:
            ValueError: If neither snapshot_id nor (snapshot_number + agent_id) provided
            NexusFileNotFoundError: If snapshot not found
            BackendError: If manifest cannot be read
        """
        with self.metadata.SessionLocal() as session:
            # Find snapshot
            if snapshot_id:
                snapshot = session.get(WorkspaceSnapshotModel, snapshot_id)
            elif snapshot_number and agent_id:
                stmt = select(WorkspaceSnapshotModel).where(
                    WorkspaceSnapshotModel.tenant_id == tenant_id,
                    WorkspaceSnapshotModel.agent_id == agent_id,
                    WorkspaceSnapshotModel.snapshot_number == snapshot_number,
                )
                snapshot = session.execute(stmt).scalar_one_or_none()
            else:
                raise ValueError("Must provide snapshot_id or (snapshot_number + agent_id)")

            if not snapshot:
                raise NexusFileNotFoundError(
                    path=f"snapshot:{snapshot_id or snapshot_number}",
                    message="Snapshot not found",
                )

            # Read manifest from CAS
            manifest_bytes = self.backend.read_content(snapshot.manifest_hash)
            manifest = json.loads(manifest_bytes.decode("utf-8"))

            # Build workspace prefix
            workspace_prefix = self._build_workspace_prefix(snapshot.tenant_id, snapshot.agent_id)

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
        agent_id: str,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List all snapshots for an agent's workspace.

        Args:
            agent_id: Agent identifier
            tenant_id: Tenant identifier (optional)
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)
        """
        with self.metadata.SessionLocal() as session:
            stmt = (
                select(WorkspaceSnapshotModel)
                .where(
                    WorkspaceSnapshotModel.tenant_id == tenant_id,
                    WorkspaceSnapshotModel.agent_id == agent_id,
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
    ) -> dict[str, Any]:
        """Compare two snapshots and return diff.

        Args:
            snapshot_id_1: First snapshot ID
            snapshot_id_2: Second snapshot ID

        Returns:
            Diff dict with keys:
                - added: List of files added in snapshot_2
                - removed: List of files removed in snapshot_2
                - modified: List of files modified between snapshots
                - unchanged: Number of unchanged files

        Raises:
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

            # Read manifests
            manifest1 = json.loads(self.backend.read_content(snap1.manifest_hash).decode("utf-8"))
            manifest2 = json.loads(self.backend.read_content(snap2.manifest_hash).decode("utf-8"))

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

    def _build_workspace_prefix(self, tenant_id: str | None, agent_id: str) -> str:
        """Build workspace path prefix.

        Args:
            tenant_id: Tenant identifier (optional)
            agent_id: Agent identifier

        Returns:
            Workspace path prefix, e.g., "/workspace/tenant1/agent1/"
        """
        if tenant_id:
            return f"/workspace/{tenant_id}/{agent_id}/"
        else:
            return f"/workspace/{agent_id}/"
