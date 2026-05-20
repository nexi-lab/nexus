"""Snapshot lookup protocols and manifest reader (Issue #1428).

Provides Protocol-based DI for snapshot retrieval and manifest reading.

SnapshotLookup: retrieve snapshot metadata by ID or latest.
ManifestReader: read file paths from a workspace snapshot manifest.

Concrete SQLAlchemy implementation lives in
``nexus.storage.repositories.snapshot_lookup`` (Issue #2189).
"""

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.contracts.workspace_manifest import manifest_storage_path

if TYPE_CHECKING:
    from nexus.contracts.filesystem import NexusFilesystem

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
    """Protocol for reading workspace snapshot manifest file paths."""

    def read_file_paths(self, workspace_path: str, snapshot_id: str) -> list[str] | None:
        """Read file paths from a workspace snapshot manifest. None on failure."""
        ...


class SyscallManifestReader:
    """ManifestReader that reads manifests through the §2.5 syscall surface.

    Workspace snapshot manifests live at
    ``/__sys__/workspace-history/{workspace_id}/{snapshot_id}.json`` (see
    nexus.contracts.workspace_manifest.manifest_storage_path — the SSOT for
    the path scheme, also used by WorkspaceManager). This reader is in the
    context_manifest brick, which cannot import nexus.services; it reaches
    the manifest bytes via sys_read, not the kernel-internal ObjectStore.

    The NexusFS handle is attached post-boot via attach_filesystem() — the
    brick tier is constructed before NexusFS exists.
    """

    def __init__(self, nexus_fs: "NexusFilesystem | None" = None) -> None:
        self._nexus_fs = nexus_fs

    def attach_filesystem(self, nexus_fs: "NexusFilesystem") -> None:
        """Attach the NexusFS handle once it exists (post-kernel boot)."""
        self._nexus_fs = nexus_fs

    def read_file_paths(self, workspace_path: str, snapshot_id: str) -> list[str] | None:
        """Read file paths from a workspace snapshot manifest via sys_read."""
        if self._nexus_fs is None:
            logger.warning("SyscallManifestReader has no NexusFS handle attached")
            return None
        from nexus.contracts.types import OperationContext

        path = manifest_storage_path(workspace_path, snapshot_id)
        try:
            ctx = OperationContext(user_id="system", groups=[], is_system=True)
            content = self._nexus_fs.sys_read(path, context=ctx)
            raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
            parsed = json.loads(raw)
            return sorted(parsed.keys())
        except Exception as exc:
            logger.warning("Failed to read manifest at %s: %s", path, exc)
            return None


__all__ = [
    "ManifestReader",
    "SnapshotLookup",
    "SyscallManifestReader",
]
