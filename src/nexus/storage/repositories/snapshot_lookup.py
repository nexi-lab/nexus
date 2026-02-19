"""SQLAlchemy-backed snapshot lookup for WorkspaceSnapshotExecutor.

Concrete implementation of SnapshotLookup protocol defined in
bricks/context_manifest/executors/snapshot_lookup_db.py.

Issue #2189: Extracted from bricks/context_manifest to remove
nexus.storage imports from the brick.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DatabaseSnapshotLookup:
    """SQLAlchemy-backed implementation of SnapshotLookup.

    Satisfies SnapshotLookup protocol via structural subtyping.
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
