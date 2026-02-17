"""Version service protocol (ops-scenario-matrix S3: History & Snapshots).

Defines the contract for file version management — retrieving specific
versions, listing history, rolling back, and diffing.

Workspace-level snapshot operations are defined separately in
``WorkspaceSnapshotProtocol`` (protocols/workspace_snapshot.py) per
Interface Segregation Principle — file versioning and workspace snapshots
have distinct consumers and different extraction timelines.

Storage Affinity: **RecordStore** (version history records) +
                  **ObjectStore** (CAS content blobs per version) +
                  **Metastore** (file metadata with etag/version pointers).

References:
    - docs/architecture/ops-scenario-matrix.md  (S3)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VersionProtocol(Protocol):
    """Service contract for file version management.

    Mirrors ``services/version_service.VersionService``.
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
