"""Version service protocol (ops-scenario-matrix S3: History & Snapshots).

Defines the contract for file version management and workspace snapshots —
retrieving specific versions, listing history, rolling back, diffing, and
creating / restoring workspace-level snapshots.

Storage Affinity: **RecordStore** (version history records) +
                  **ObjectStore** (CAS content blobs per version) +
                  **Metastore** (file metadata with etag/version pointers).

References:
    - docs/architecture/ops-scenario-matrix.md  (S3)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VersionProtocol(Protocol):
    """Service contract for file version management and workspace snapshots.

    File-level operations mirror ``services/version_service.VersionService``.
    Workspace-level operations mirror the snapshot helpers currently inlined
    on the NexusFS god object (``workspace_snapshot``, ``workspace_restore``,
    ``workspace_log``, ``workspace_diff``).
    """

    # ── File versioning ───────────────────────────────────────────────

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

    # ── Workspace snapshots ───────────────────────────────────────────

    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]: ...

    def workspace_log(
        self,
        workspace_path: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]: ...
