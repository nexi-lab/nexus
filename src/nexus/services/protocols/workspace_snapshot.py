"""Workspace snapshot protocol (ops-scenario-matrix S3: History & Snapshots).

Defines the contract for workspace-level snapshot operations — creating,
restoring, listing, and diffing workspace snapshots.

Split from ``VersionProtocol`` per Interface Segregation Principle:
file versioning and workspace snapshots have distinct consumers and
different extraction timelines.  File versioning is already extracted to
``VersionService``; workspace snapshots are still inlined on NexusFS.

Storage Affinity: **RecordStore** (snapshot metadata) +
                  **ObjectStore** (snapshot content blobs) +
                  **Metastore** (workspace-level metadata).

References:
    - docs/architecture/ops-scenario-matrix.md  (S3)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceSnapshotProtocol(Protocol):
    """Service contract for workspace snapshot operations.

    Currently mirrors the snapshot helpers inlined on the NexusFS god object
    (``workspace_snapshot``, ``workspace_restore``, ``workspace_log``,
    ``workspace_diff``).  Will be backed by a dedicated service once
    workspace snapshot logic is extracted from NexusFS.
    """

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
