"""WorkspaceSnapshotExecutor — resolve workspace_snapshot sources (Issue #1428).

Retrieves workspace snapshot metadata (and optionally file tree) for injection
into agent context. Supports both specific snapshot IDs and "latest" resolution.

Performance:
    - Blocking DB queries run in thread pool to avoid blocking the event loop.
    - File tree capped at MAX_TREE_FILES to prevent context explosion.
    - Thread pool is configurable via constructor (14B).
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Executor
from typing import Any

from nexus.services.context_manifest.executors.executor_utils import (
    WorkspaceSnapshotSourceProtocol,
    resolve_source_template,
)
from nexus.services.context_manifest.executors.snapshot_lookup_db import (
    ManifestReader,
    SnapshotLookup,
)
from nexus.services.context_manifest.models import ContextSourceProtocol, SourceResult

logger = logging.getLogger(__name__)

MAX_TREE_FILES = 200


class WorkspaceSnapshotExecutor:
    """Execute workspace_snapshot sources by loading snapshot metadata.

    Args:
        snapshot_lookup: A SnapshotLookup protocol implementation for
            retrieving snapshot metadata from the database.
        manifest_reader: Optional ManifestReader for loading file trees
            from CAS. If not provided, file_tree is omitted from results.
        thread_pool: Optional thread pool for blocking I/O. Defaults to
            the event loop's default executor.
    """

    def __init__(
        self,
        snapshot_lookup: SnapshotLookup,
        manifest_reader: ManifestReader | None = None,
        thread_pool: Executor | None = None,
    ) -> None:
        self._snapshot_lookup = snapshot_lookup
        self._manifest_reader = manifest_reader
        self._thread_pool = thread_pool

    async def execute(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Resolve a workspace_snapshot source by loading snapshot metadata.

        Delegates to thread pool to avoid blocking the event loop with
        synchronous database I/O.

        Args:
            source: A WorkspaceSnapshotSource instance (accessed via protocol).
            variables: Template variables for snapshot_id substitution.

        Returns:
            SourceResult with snapshot metadata, or error on failure.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._thread_pool, self._execute_sync, source, variables)

    def _execute_sync(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Synchronous implementation of workspace snapshot resolution."""
        start = time.monotonic()

        # Extract snapshot_id via typed protocol (6A)
        snapshot_id: str = (
            source.snapshot_id
            if isinstance(source, WorkspaceSnapshotSourceProtocol)
            else getattr(source, "snapshot_id", "latest")
        )

        # Resolve template variables in snapshot_id (5A — shared helper)
        snapshot_id, err = resolve_source_template(snapshot_id, variables, source, start)
        if err is not None:
            return err

        # Resolve snapshot
        try:
            snapshot = self._resolve_snapshot(snapshot_id, variables)
        except _SnapshotError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult.error(
                source_type=source.type,
                source_name=source.source_name,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

        # Build result data
        data: dict[str, Any] = {
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_number": snapshot.get("snapshot_number"),
            "workspace_path": snapshot.get("workspace_path"),
            "file_count": snapshot.get("file_count", 0),
            "total_size_bytes": snapshot.get("total_size_bytes", 0),
            "description": snapshot.get("description"),
            "created_by": snapshot.get("created_by"),
            "tags": snapshot.get("tags", []),
            "created_at": snapshot.get("created_at"),
        }

        # Optionally load file tree from manifest
        manifest_hash = snapshot.get("manifest_hash")
        if self._manifest_reader is not None and manifest_hash:
            try:
                file_paths = self._manifest_reader.read_file_paths(manifest_hash)
                if file_paths is not None:
                    total_files = len(file_paths)
                    capped_paths = file_paths[:MAX_TREE_FILES]
                    data["file_tree"] = capped_paths
                    data["file_tree_total"] = total_files
                    data["file_tree_capped"] = total_files > MAX_TREE_FILES
            except Exception as exc:
                # Graceful degradation — omit file_tree on reader failure
                logger.warning("Failed to read file tree for manifest %s: %s", manifest_hash, exc)

        elapsed_ms = (time.monotonic() - start) * 1000

        return SourceResult.ok(
            source_type=source.type,
            source_name=source.source_name,
            data=data,
            elapsed_ms=elapsed_ms,
        )

    def _resolve_snapshot(self, snapshot_id: str, variables: dict[str, str]) -> dict[str, Any]:
        """Resolve a snapshot by ID or 'latest'.

        Raises:
            _SnapshotError: If the snapshot cannot be found or resolved.
        """
        if snapshot_id == "latest":
            workspace_root = variables.get("workspace.root")
            if not workspace_root:
                raise _SnapshotError(
                    "Cannot resolve 'latest' snapshot: 'workspace.root' variable not provided"
                )
            snapshot = self._snapshot_lookup.get_latest_snapshot(workspace_root)
            if snapshot is None:
                raise _SnapshotError(f"No snapshots found for workspace '{workspace_root}'")
            return snapshot

        snapshot = self._snapshot_lookup.get_snapshot(snapshot_id)
        if snapshot is None:
            raise _SnapshotError(f"Snapshot '{snapshot_id}' not found")
        return snapshot


class _SnapshotError(Exception):
    """Internal error for snapshot resolution failures."""
