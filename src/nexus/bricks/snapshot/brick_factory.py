"""Auto-discoverable brick factory for TransactionalSnapshotService (Issue #2180)."""

from __future__ import annotations

from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "independent"
RESULT_KEY = "snapshot_service"


def create(ctx: Any, _kernel: dict[str, Any]) -> Any:
    """Create TransactionalSnapshotService. Lazy imports inside."""
    from nexus.bricks.snapshot.service import TransactionalSnapshotService
    from nexus.core.metadata import FileMetadata

    return TransactionalSnapshotService(
        session_factory=ctx.session_factory,
        cas_store=ctx.backend,
        metadata_store=ctx.metadata_store,
        metadata_factory=FileMetadata,
    )
