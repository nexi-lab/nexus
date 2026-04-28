"""Version service protocol (ops-scenario-matrix S3: History & Snapshots).

Defines the contract for file version management and workspace snapshots —
retrieving specific versions, listing history, rolling back, diffing, and
creating / restoring workspace-level snapshots.

Storage Affinity: **RecordStore** (version history records) +
                  **ObjectStore** (CAS content blobs per version) +
                  **Metastore** (file metadata with content_id/version pointers).

References:
    - docs/architecture/ops-scenario-matrix.md  (S3)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VersionProtocol(Protocol):
    """Service contract for file version management.

    File-level operations mirror ``services/version_service.VersionService``.

    Note: Workspace-level snapshot operations (workspace_snapshot,
    workspace_restore, workspace_log, workspace_diff) formerly lived here
    but have been migrated to TransactionalSnapshotProtocol (Issue #1752).
    """

    async def get_version(
        self,
        path: str,
        version: int,
        context: Any | None = None,
    ) -> bytes: ...

    async def list_versions(
        self,
        path: str,
        context: Any | None = None,
    ) -> list[dict[str, Any]]: ...

    async def rollback(
        self,
        path: str,
        version: int,
        context: Any | None = None,
    ) -> None: ...

    async def diff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: Any | None = None,
    ) -> dict[str, Any] | str: ...
