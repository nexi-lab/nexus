"""Database-backed snapshot lookup for WorkspaceSnapshotExecutor (Issue #1428).

Provides Protocol-based DI for snapshot retrieval and manifest reading,
plus concrete implementations backed by SQLAlchemy and CAS.

SnapshotLookup: retrieve snapshot metadata by ID or latest.
ManifestReader: read file paths from a CAS-stored workspace manifest.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class SnapshotLookup(Protocol):
    """Protocol for workspace snapshot retrieval."""

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Get snapshot metadata by ID. Returns None if not found."""
        ...

    def get_latest_snapshot(self, workspace_path: str) -> dict[str, Any] | None:
        """Get the most recent snapshot for a workspace path. Returns None if none exist."""
        ...


@runtime_checkable
class ManifestReader(Protocol):
    """Protocol for reading workspace manifest file paths from CAS."""

    def read_file_paths(self, manifest_hash: str) -> list[str] | None:
        """Read file paths from a CAS-stored manifest. Returns None on failure."""
        ...


class DatabaseSnapshotLookup:
    """SQLAlchemy-backed implementation of SnapshotLookup.

    Queries the workspace_snapshots table for snapshot metadata.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Get snapshot by ID."""
        from nexus.storage.models.filesystem import WorkspaceSnapshotModel

        with self._session_factory() as session:
            model = session.get(WorkspaceSnapshotModel, snapshot_id)
            if model is None:
                return None
            return _model_to_dict(model)

    def get_latest_snapshot(self, workspace_path: str) -> dict[str, Any] | None:
        """Get the most recent snapshot for a workspace path."""
        from sqlalchemy import select

        from nexus.storage.models.filesystem import WorkspaceSnapshotModel

        with self._session_factory() as session:
            stmt = (
                select(WorkspaceSnapshotModel)
                .where(WorkspaceSnapshotModel.workspace_path == workspace_path)
                .order_by(WorkspaceSnapshotModel.created_at.desc())
                .limit(1)
            )
            result = session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None
            return _model_to_dict(model)


class CASManifestReader:
    """CAS-backed implementation of ManifestReader.

    Reads a workspace manifest from content-addressable storage and
    extracts the sorted list of file paths.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def read_file_paths(self, manifest_hash: str) -> list[str] | None:
        """Read file paths from a CAS-stored workspace manifest."""
        try:
            content = self._backend.read_content(manifest_hash)
            if content is None:
                logger.warning("Manifest hash %s not found in CAS", manifest_hash)
                return None

            from nexus.core.workspace_manifest import WorkspaceManifest

            manifest = WorkspaceManifest.from_json(
                content if isinstance(content, bytes) else content.encode("utf-8")
            )
            return sorted(manifest.paths())
        except Exception as exc:
            logger.warning("Failed to read manifest %s: %s", manifest_hash, exc)
            return None


def _model_to_dict(model: Any) -> dict[str, Any]:
    """Convert a WorkspaceSnapshotModel to a dict matching WorkspaceManager schema."""
    tags: list[str] = []
    if model.tags:
        try:
            tags = json.loads(model.tags) if isinstance(model.tags, str) else model.tags
        except (json.JSONDecodeError, TypeError):
            tags = []

    return {
        "snapshot_id": model.snapshot_id,
        "workspace_path": model.workspace_path,
        "snapshot_number": model.snapshot_number,
        "manifest_hash": model.manifest_hash,
        "file_count": model.file_count,
        "total_size_bytes": model.total_size_bytes,
        "description": model.description,
        "created_by": model.created_by,
        "tags": tags,
        "created_at": model.created_at.isoformat() if model.created_at else None,
    }
