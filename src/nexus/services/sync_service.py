"""Sync Service - Metadata and content synchronization.

This service handles synchronization of metadata and content from
connector backends to the Nexus database.

Phase 2: Mount Mixin Refactoring
Extracted from: nexus_fs_mounts.py (~800 lines of sync logic)

All methods are synchronous. FastAPI auto-wraps with to_thread.

Example:
    ```python
    sync_service = SyncService(gateway)
    ctx = SyncContext(mount_point="/mnt/gcs", recursive=True)
    result = sync_service.sync_mount(ctx)
    print(f"Synced {result.files_created} files")
    ```
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.context_utils import get_user_identity, get_zone_id
from nexus.services.change_log_store import ChangeLogEntry, ChangeLogStore

if TYPE_CHECKING:
    from nexus.backends.backend import FileInfo
    from nexus.core.permissions import OperationContext
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


# Type alias for progress callback: (files_scanned: int, current_path: str) -> None
ProgressCallback = Callable[[int, str], None]


@dataclass
class SyncContext:
    """Context object for sync_mount operations.

    Groups all parameters needed for syncing a mount to reduce parameter passing.
    """

    mount_point: str | None
    path: str | None = None
    recursive: bool = True
    dry_run: bool = False
    sync_content: bool = True
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    generate_embeddings: bool = False
    context: OperationContext | None = None
    progress_callback: ProgressCallback | None = None
    # Issue #1127: Delta sync support
    full_sync: bool = False  # Force full scan, bypassing delta checks


@dataclass
class SyncResult:
    """Result of a sync operation."""

    files_scanned: int = 0
    files_created: int = 0
    files_updated: int = 0
    files_deleted: int = 0
    # Issue #1127: Delta sync metrics
    files_skipped: int = 0  # Files skipped due to no changes (delta sync)
    cache_synced: int = 0
    cache_bytes: int = 0
    cache_skipped: int = 0
    embeddings_generated: int = 0
    errors: list[str] = field(default_factory=list)
    # For sync_all_mounts
    mounts_synced: int = 0
    mounts_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


class SyncService:
    """Handles metadata and content synchronization (SYNC).

    All methods are synchronous. FastAPI auto-wraps with to_thread.

    AI-Friendly Design:
    - All NexusFS access via self._gw
    - Clear step-by-step sync process
    - BFS traversal for memory efficiency
    """

    # Memory-efficient chunking: flush batch every N paths
    PATHS_CHUNK_SIZE = 10000

    def __init__(self, gateway: NexusFSGateway):
        """Initialize sync service.

        Args:
            gateway: NexusFSGateway for NexusFS access
        """
        self._gw = gateway
        # Issue #1127: Initialize change log store for delta sync
        self._change_log = ChangeLogStore(gateway)
        # Issue #1127: Per-mount sync locks to prevent concurrent races
        self._mount_locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()

    def _get_mount_lock(self, mount_point: str) -> threading.Lock:
        """Get or create a lock for a specific mount point.

        Thread-safe: uses a guard lock to protect the mount_locks dict.

        Args:
            mount_point: Mount point path

        Returns:
            Lock for the given mount point
        """
        with self._lock_guard:
            if mount_point not in self._mount_locks:
                self._mount_locks[mount_point] = threading.Lock()
            return self._mount_locks[mount_point]

    def _resolve_zone_id(self, ctx: SyncContext) -> str | None:
        """Resolve zone ID from context or mount point path.

        Tries context first, then falls back to extracting zone from mount point
        path pattern ``/zone/{zone_id}/...``.

        Args:
            ctx: SyncContext with context and mount_point

        Returns:
            Zone ID string or None if not determinable
        """
        zone_id = get_zone_id(ctx.context) if ctx.context else None
        if not zone_id and ctx.mount_point:
            match = re.match(r"^/zone/([^/]+)/", ctx.mount_point)
            if match:
                zone_id = match.group(1)
        return zone_id

    def sync_mount(self, ctx: SyncContext) -> SyncResult:
        """Sync metadata and content from connector backend(s).

        Main entry point for sync operations.

        Args:
            ctx: SyncContext with all sync parameters

        Returns:
            SyncResult with statistics

        Raises:
            PermissionError: If user lacks read permission on mount
            ValueError: If mount_point doesn't exist
            RuntimeError: If backend doesn't support listing
        """
        # Step 1: If no mount_point, sync all connector mounts
        if ctx.mount_point is None:
            return self._sync_all_mounts(ctx)

        # Step 1.5: Acquire per-mount lock (non-blocking to avoid queueing)
        lock = self._get_mount_lock(ctx.mount_point)
        if not lock.acquire(blocking=False):
            logger.warning(f"[SYNC_MOUNT] Sync already in progress for {ctx.mount_point}")
            result = SyncResult()
            result.errors.append(f"Sync already in progress for {ctx.mount_point}")
            return result

        try:
            # Step 2: Check permission before syncing
            if not self._check_permission(ctx.mount_point, "read", ctx.context):
                raise PermissionError(f"Cannot sync mount {ctx.mount_point}: no read permission")

            result = SyncResult()

            # Step 3: Validate mount and get backend
            backend = self._validate_mount(ctx)

            # Step 4: Sync metadata (BFS traversal)
            files_found = self._sync_metadata(ctx, backend, result)

            # Step 5: Handle deletions
            self._sync_deletions(ctx, backend, files_found, result)

            # Step 6: Sync content cache
            self._sync_content(ctx, backend, result)

            # Issue #1127: Log delta sync summary
            if result.files_skipped > 0:
                logger.info(
                    f"[DELTA_SYNC] Summary for {ctx.mount_point}: "
                    f"scanned={result.files_scanned}, created={result.files_created}, "
                    f"skipped={result.files_skipped} "
                    f"(delta sync saved {result.files_skipped} file operations)"
                )

            return result
        finally:
            lock.release()

    def _validate_mount(self, ctx: SyncContext) -> Any:
        """Validate mount exists and supports sync.

        Args:
            ctx: SyncContext with mount_point

        Returns:
            Backend instance

        Raises:
            ValueError: If mount not found
            RuntimeError: If backend doesn't support list_dir
        """
        assert ctx.mount_point is not None

        mount = self._gw.router.get_mount(ctx.mount_point)
        if not mount:
            raise ValueError(f"Mount not found: {ctx.mount_point}")

        backend = mount.backend
        backend_name = type(backend).__name__

        if not hasattr(backend, "list_dir"):
            raise RuntimeError(
                f"Backend {backend_name} does not support metadata sync. "
                f"Only connector-style backends (e.g., gcs_connector) can be synced."
            )

        # Mount directory entry is created by add_mount() -> _setup_mount_point()

        return backend

    def _sync_all_mounts(self, ctx: SyncContext) -> SyncResult:
        """Sync all connector mounts when mount_point is None.

        Args:
            ctx: SyncContext (mount_point should be None)

        Returns:
            Aggregated SyncResult
        """
        logger.info("[SYNC_MOUNT] No mount_point specified, syncing all connector mounts")

        result = SyncResult()
        all_mounts = self._gw.list_mounts()

        for mount_info in all_mounts:
            mp = mount_info.get("mount_point", "")
            backend = mount_info.get("backend")

            # Only sync connector-style backends (those with list_dir support)
            if not backend or not hasattr(backend, "list_dir"):
                backend_type = mount_info.get("backend_type", "unknown")
                logger.info(
                    f"[SYNC_MOUNT] Skipping {mp} ({backend_type}) - not a connector backend"
                )
                result.mounts_skipped += 1
                continue

            logger.info(f"[SYNC_MOUNT] Syncing mount: {mp}")
            try:
                # Create new context for this mount
                mount_ctx = SyncContext(
                    mount_point=mp,
                    path=ctx.path,
                    recursive=ctx.recursive,
                    dry_run=ctx.dry_run,
                    sync_content=ctx.sync_content,
                    include_patterns=ctx.include_patterns,
                    exclude_patterns=ctx.exclude_patterns,
                    generate_embeddings=ctx.generate_embeddings,
                    context=ctx.context,
                    progress_callback=ctx.progress_callback,
                )

                mount_result = self.sync_mount(mount_ctx)

                # Aggregate stats
                result.mounts_synced += 1
                result.files_scanned += mount_result.files_scanned
                result.files_created += mount_result.files_created
                result.files_updated += mount_result.files_updated
                result.files_deleted += mount_result.files_deleted
                result.files_skipped += mount_result.files_skipped  # Issue #1127
                result.cache_synced += mount_result.cache_synced
                result.cache_bytes += mount_result.cache_bytes
                result.embeddings_generated += mount_result.embeddings_generated

                # Prefix errors with mount point
                for error in mount_result.errors:
                    result.errors.append(f"[{mp}] {error}")

            except Exception as e:
                result.errors.append(f"[{mp}] Failed to sync: {e}")
                logger.warning(f"[SYNC_MOUNT] Failed to sync {mp}: {e}")

        logger.info(
            f"[SYNC_MOUNT] All mounts sync complete: "
            f"{result.mounts_synced} synced, {result.mounts_skipped} skipped"
        )
        return result

    def _sync_metadata(
        self,
        ctx: SyncContext,
        backend: Any,
        result: SyncResult,
    ) -> set[str]:
        """Scan backend and sync metadata using BFS traversal.

        Args:
            ctx: SyncContext
            backend: Backend instance
            result: SyncResult to update

        Returns:
            Set of files found in backend
        """

        assert ctx.mount_point is not None

        files_found: set[str] = set()
        paths_needing_tuples: list[str] = []
        total_tuples_created = 0

        # Get created_by from context
        created_by = self._get_created_by(ctx)

        def flush_parent_tuples_batch() -> None:
            """Flush accumulated paths and create parent tuples in batch."""
            nonlocal paths_needing_tuples, total_tuples_created

            if paths_needing_tuples and self._gw.hierarchy_enabled:
                try:
                    zone_id = self._resolve_zone_id(ctx)
                    logger.info(
                        f"[SYNC_MOUNT] Flushing batch: creating parent tuples for "
                        f"{len(paths_needing_tuples)} files (zone_id={zone_id})"
                    )
                    created = self._gw.ensure_parent_tuples_batch(
                        paths_needing_tuples, zone_id=zone_id
                    )
                    total_tuples_created += created
                    logger.info(
                        f"[SYNC_MOUNT] Flushed batch: created {created} parent tuples "
                        f"(total so far: {total_tuples_created})"
                    )
                    paths_needing_tuples.clear()
                except Exception as e:
                    logger.warning(
                        f"Failed to flush parent tuples batch: {type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    paths_needing_tuples.clear()

        # Determine starting path for scan
        start_virtual_path, start_backend_path = self._get_start_paths(ctx)

        # Issue #1127: Pre-fetch all cached change log entries for this mount
        # Eliminates per-file DB round-trips (~100x speedup for large mounts)
        zone_id = self._resolve_zone_id(ctx)
        cached_entries: dict[str, ChangeLogEntry] = {}
        if not ctx.full_sync and hasattr(backend, "get_file_info"):
            cached_entries = self._change_log.get_change_logs_batch(
                backend_name=backend.name,
                zone_id=zone_id or "default",
                path_prefix=ctx.mount_point or "",
            )
            if cached_entries:
                logger.info(
                    f"[DELTA_SYNC] Pre-fetched {len(cached_entries)} cached entries "
                    f"for {ctx.mount_point}"
                )

        # Collect change log upserts for batch flush
        pending_upserts: list[ChangeLogEntry] = []

        def flush_pending_upserts() -> None:
            """Flush accumulated change log upserts in a single transaction."""
            if pending_upserts:
                self._change_log.upsert_change_logs_batch(list(pending_upserts))
                pending_upserts.clear()

        # Check if this is a single file sync
        if ctx.path:
            is_single_file = self._check_single_file(backend, start_backend_path, ctx.context)
            if is_single_file:
                found = self._sync_single_file(
                    ctx,
                    backend,
                    start_virtual_path,
                    start_backend_path,
                    created_by,
                    result,
                    files_found,
                    paths_needing_tuples,
                    flush_parent_tuples_batch,
                    cached_entries,
                    pending_upserts,
                )
                flush_pending_upserts()
                return found

        # BFS traversal using deque
        queue: deque[tuple[str, str]] = deque([(start_virtual_path, start_backend_path)])

        while queue:
            virtual_path, backend_path = queue.popleft()

            try:
                entries = backend.list_dir(backend_path, context=ctx.context)

                for entry_name in entries:
                    is_dir = entry_name.endswith("/")
                    entry_name = entry_name.rstrip("/")

                    # Construct paths
                    if virtual_path == ctx.mount_point:
                        entry_virtual_path = f"{ctx.mount_point}/{entry_name}"
                    else:
                        entry_virtual_path = f"{virtual_path}/{entry_name}"

                    if backend_path:
                        entry_backend_path = f"{backend_path}/{entry_name}"
                    else:
                        entry_backend_path = entry_name

                    if is_dir:
                        # Process directory
                        self._sync_directory(
                            ctx,
                            backend,
                            entry_virtual_path,
                            entry_backend_path,
                            created_by,
                            result,
                            files_found,
                            paths_needing_tuples,
                        )

                        # Add to queue for recursive traversal
                        if ctx.recursive:
                            queue.append((entry_virtual_path, entry_backend_path))

                        # Flush batch if needed
                        if len(paths_needing_tuples) >= self.PATHS_CHUNK_SIZE:
                            flush_parent_tuples_batch()
                    else:
                        # Process file
                        self._sync_file(
                            ctx,
                            backend,
                            entry_virtual_path,
                            entry_backend_path,
                            created_by,
                            result,
                            files_found,
                            paths_needing_tuples,
                            cached_entries,
                            pending_upserts,
                        )

                        # Flush batches if needed
                        if len(paths_needing_tuples) >= self.PATHS_CHUNK_SIZE:
                            flush_parent_tuples_batch()
                        if len(pending_upserts) >= self.PATHS_CHUNK_SIZE:
                            flush_pending_upserts()

            except Exception as e:
                error_msg = f"Failed to scan {virtual_path}: {e}"
                result.errors.append(error_msg)
                logger.warning(error_msg)

        # Final flush
        if paths_needing_tuples:
            flush_parent_tuples_batch()
        flush_pending_upserts()

        if total_tuples_created > 0:
            logger.info(
                f"[SYNC_MOUNT] Parent tuple creation complete: "
                f"{total_tuples_created} tuples created total"
            )

        return files_found

    def _sync_file(
        self,
        ctx: SyncContext,
        backend: Any,
        virtual_path: str,
        backend_path: str,
        created_by: str | None,
        result: SyncResult,
        files_found: set[str],
        paths_needing_tuples: list[str],
        cached_entries: dict[str, ChangeLogEntry] | None = None,
        pending_upserts: list[ChangeLogEntry] | None = None,
    ) -> None:
        """Sync a single file entry.

        Issue #1127: Implements delta sync with change tracking.
        Skips unchanged files based on size, mtime, or backend_version comparison.

        Args:
            ctx: SyncContext
            backend: Backend instance
            virtual_path: Virtual file path
            backend_path: Backend file path
            created_by: Creator identifier
            result: SyncResult to update
            files_found: Set to track found files
            paths_needing_tuples: List to collect paths for batch tuple creation
            cached_entries: Pre-fetched change log entries (batch optimization)
            pending_upserts: Accumulator for batch upsert (batch optimization)
        """
        from nexus.core._metadata_generated import FileMetadata

        # Apply pattern filtering
        if not self._matches_patterns(virtual_path, ctx):
            return

        result.files_scanned += 1

        # Call progress callback if provided
        if ctx.progress_callback:
            try:
                ctx.progress_callback(result.files_scanned, virtual_path)
            except Exception as cb_error:
                from nexus.services.sync_job_manager import SyncCancelled

                if isinstance(cb_error, SyncCancelled):
                    raise
                logger.warning(f"Progress callback error: {cb_error}")

        files_found.add(virtual_path)

        if ctx.dry_run:
            return

        # Extract zone_id early (needed for delta sync and metadata creation)
        zone_id = self._resolve_zone_id(ctx)

        # Issue #1127: Delta sync - check if file has changed
        # Always fetch file_info to populate change log on first sync (bootstrap).
        # Only compare against cached entry when one exists.
        file_info = None
        if not ctx.full_sync and hasattr(backend, "get_file_info"):
            try:
                file_info_response = backend.get_file_info(backend_path, context=ctx.context)
                if file_info_response.success and file_info_response.data:
                    file_info = file_info_response.data

                    # Compare against cached entry if available (skip unchanged files)
                    # Use pre-fetched batch cache when available, else fall back to single query
                    if cached_entries is not None:
                        cached = cached_entries.get(virtual_path)
                    else:
                        cached = self._change_log.get_change_log(
                            virtual_path, backend.name, zone_id or "default"
                        )
                    if cached and self._file_unchanged(file_info, cached):
                        result.files_skipped += 1
                        logger.debug(
                            f"[DELTA_SYNC] Skipping unchanged: {virtual_path} "
                            f"(size={file_info.size}, version={file_info.backend_version})"
                        )
                        return

                    if cached:
                        logger.debug(
                            f"[DELTA_SYNC] File changed: {virtual_path} "
                            f"(old_version={cached.backend_version}, "
                            f"new_version={file_info.backend_version})"
                        )
            except Exception as e:
                # Delta check failed - proceed with full sync for this file
                logger.warning(f"[DELTA_SYNC] Change detection failed for {virtual_path}: {e}")

        # Check if file exists in metadata
        existing_meta = self._gw.metadata_get(virtual_path)

        if not existing_meta:
            try:
                if not zone_id:
                    raise ValueError(
                        f"Cannot sync file {virtual_path}: zone_id not found in context or mount point path"
                    )

                now = datetime.now(UTC)

                # Get file size from backend if available (use cached file_info if we have it)
                if file_info and file_info.size is not None:
                    file_size = file_info.size
                else:
                    file_size = self._get_file_size(backend, backend_path, ctx)

                # Issue #1126: etag=None during metadata sync
                # Content hash is computed later by cache_mixin.sync() when content is read
                # This avoids the bug of storing path hash instead of content hash
                meta = FileMetadata(
                    path=virtual_path,
                    backend_name=backend.name,
                    physical_path=backend_path,
                    size=file_size,
                    etag=None,
                    created_at=now,
                    modified_at=now,
                    version=1,
                    created_by=created_by,
                    zone_id=zone_id,
                )

                self._gw.metadata_put(meta)
                result.files_created += 1

                # Collect for batch parent tuple creation
                if self._gw.hierarchy_enabled:
                    paths_needing_tuples.append(virtual_path)

                # Issue #1127: Update change log after successful sync
                if file_info:
                    self._enqueue_change_log_upsert(
                        pending_upserts,
                        virtual_path,
                        backend.name,
                        zone_id or "default",
                        file_info,
                    )

            except Exception as e:
                result.errors.append(f"Failed to add {virtual_path}: {e}")
        else:
            # File exists - check if it needs updating (for existing files)
            # Issue #1127: Update change log even for existing files
            if file_info:
                self._enqueue_change_log_upsert(
                    pending_upserts,
                    virtual_path,
                    backend.name,
                    zone_id or "default",
                    file_info,
                )

    def _enqueue_change_log_upsert(
        self,
        pending_upserts: list[ChangeLogEntry] | None,
        path: str,
        backend_name: str,
        zone_id: str,
        file_info: FileInfo,
    ) -> None:
        """Enqueue a change log upsert for batch flush, or upsert immediately.

        When pending_upserts is provided, appends to the list for batch processing.
        Otherwise falls back to immediate single upsert for backward compatibility.

        Args:
            pending_upserts: Batch accumulator list, or None for immediate upsert
            path: Virtual file path
            backend_name: Backend identifier
            zone_id: Zone ID
            file_info: File info from backend
        """
        entry = ChangeLogEntry(
            path=path,
            backend_name=backend_name,
            size_bytes=file_info.size,
            mtime=file_info.mtime,
            backend_version=file_info.backend_version,
            content_hash=file_info.content_hash,
        )
        if pending_upserts is not None:
            pending_upserts.append(entry)
        else:
            self._change_log.upsert_change_log(
                path=path,
                backend_name=backend_name,
                zone_id=zone_id,
                size_bytes=file_info.size,
                mtime=file_info.mtime,
                backend_version=file_info.backend_version,
                content_hash=file_info.content_hash,
            )

    def _file_unchanged(self, file_info: FileInfo, cached: ChangeLogEntry) -> bool:
        """Check if file is unchanged based on rsync-style comparison (Issue #1127).

        Uses tiered comparison strategy:
        1. Backend version (GCS generation, S3 version ID) - most reliable
        2. Size + mtime - quick check, catches most changes
        3. Content hash - fallback if available

        Args:
            file_info: Current file info from backend
            cached: Cached change log entry from previous sync

        Returns:
            True if file is unchanged, False if changed or unknown
        """
        # Strategy 1: Backend version comparison (most reliable)
        if file_info.backend_version and cached.backend_version:
            return file_info.backend_version == cached.backend_version

        # Strategy 2: Size + mtime comparison (rsync quick check)
        if (
            file_info.size is not None
            and cached.size_bytes is not None
            and file_info.size != cached.size_bytes
        ):
            return False  # Size changed

        if file_info.mtime and cached.mtime:
            # Compare timestamps with 1-second tolerance for filesystem precision
            time_diff = abs((file_info.mtime - cached.mtime).total_seconds())
            if time_diff > 1.0:
                return False  # mtime changed

            # If we get here with matching size and mtime, consider unchanged
            if file_info.size is not None and cached.size_bytes is not None:
                return True

        # Strategy 3: Content hash fallback
        if file_info.content_hash and cached.content_hash:
            return file_info.content_hash == cached.content_hash

        # Cannot determine - assume changed to be safe
        return False

    def _sync_directory(
        self,
        ctx: SyncContext,
        backend: Any,
        virtual_path: str,
        backend_path: str,
        created_by: str | None,
        result: SyncResult,
        files_found: set[str],
        paths_needing_tuples: list[str],
    ) -> None:
        """Sync a directory entry.

        Args:
            ctx: SyncContext
            backend: Backend instance
            virtual_path: Virtual directory path
            backend_path: Backend directory path
            created_by: Creator identifier
            result: SyncResult to update
            files_found: Set to track found paths
            paths_needing_tuples: List for batch tuple creation
        """
        from nexus.core._metadata_generated import FileMetadata

        files_found.add(virtual_path)

        if ctx.dry_run:
            return

        existing_meta = self._gw.metadata_get(virtual_path)
        if existing_meta:
            return

        try:
            zone_id = self._resolve_zone_id(ctx)

            if not zone_id:
                raise ValueError(
                    f"Cannot sync directory {virtual_path}: zone_id not found in context or mount point path"
                )

            now = datetime.now(UTC)

            # Issue #1126: etag=None for directories (no content to hash)
            dir_meta = FileMetadata(
                path=virtual_path,
                backend_name=backend.name,
                physical_path=backend_path,
                size=0,
                etag=None,
                mime_type="inode/directory",
                created_at=now,
                modified_at=now,
                version=1,
                created_by=created_by,
                zone_id=zone_id,
            )

            self._gw.metadata_put(dir_meta)

            if self._gw.hierarchy_enabled:
                paths_needing_tuples.append(virtual_path)

        except Exception as e:
            result.errors.append(f"Failed to create directory marker for {virtual_path}: {e}")
            logger.warning(f"Failed to create directory marker for {virtual_path}: {e}")

    def _sync_deletions(
        self,
        ctx: SyncContext,
        backend: Any,
        files_found: set[str],
        result: SyncResult,
    ) -> None:
        """Handle file deletions - remove files no longer in backend.

        Only performs deletion check when syncing from root (path=None).
        Also cleans up stale BackendChangeLogModel entries to prevent
        false skips when files are re-created (Issue #1127).

        Args:
            ctx: SyncContext
            backend: Backend instance (for backend_name in change log cleanup)
            files_found: Set of files found during metadata scan
            result: SyncResult to update
        """
        if ctx.dry_run or ctx.path is not None or ctx.mount_point is None:
            return

        try:
            # Get other mount points to exclude
            other_mount_points = set()
            try:
                all_mounts = self._gw.list_mounts()
                for m in all_mounts:
                    mp = m.get("mount_point", "")
                    if mp and mp != ctx.mount_point and mp != "/":
                        other_mount_points.add(mp)
            except Exception:
                pass

            # Resolve zone_id for change log cleanup
            zone_id = self._resolve_zone_id(ctx)

            # List all files in metadata under this mount
            existing_metas = self._gw.metadata_list(prefix=ctx.mount_point, recursive=True)

            for meta in existing_metas:
                existing_path = meta.path
                # Skip the mount point itself
                if existing_path == ctx.mount_point:
                    continue

                # Skip if found in backend
                if existing_path in files_found:
                    continue

                # Skip if belongs to another mount
                belongs_to_other = any(
                    existing_path.startswith(mp + "/") or existing_path == mp
                    for mp in other_mount_points
                )
                if belongs_to_other:
                    continue

                # Delete from metadata
                try:
                    existing_meta = self._gw.metadata_get(existing_path)
                    if existing_meta:
                        logger.info(
                            f"[SYNC_MOUNT] Deleting file no longer in backend: {existing_path}"
                        )
                        self._gw.metadata_delete(existing_path)
                        result.files_deleted += 1

                        # Issue #1127: Clean up stale change log entry
                        self._change_log.delete_change_log(
                            existing_path, backend.name, zone_id or "default"
                        )
                except Exception as e:
                    result.errors.append(f"Failed to delete {existing_path}: {e}")
                    logger.warning(f"Failed to delete {existing_path}: {e}")

        except Exception as e:
            result.errors.append(f"Failed to check for deletions: {e}")
            logger.warning(f"Failed to check for deletions: {e}")

    def _sync_content(
        self,
        ctx: SyncContext,
        backend: Any,
        result: SyncResult,
    ) -> None:
        """Sync content to cache if requested.

        Delegates to backend.sync() method (from CacheConnectorMixin).

        Args:
            ctx: SyncContext
            backend: Backend instance
            result: SyncResult to update
        """
        if not ctx.sync_content or ctx.dry_run:
            return

        if not hasattr(backend, "sync"):
            logger.info(
                f"[SYNC_MOUNT] Backend {type(backend).__name__} does not support sync(), "
                "skipping content cache population"
            )
            return

        logger.info("[SYNC_MOUNT] Delegating to backend.sync() for cache population")

        try:
            from nexus.backends.cache_mixin import SyncResult as CacheSyncResult

            # Determine path for cache sync
            cache_sync_path = None
            if ctx.path and ctx.mount_point:
                if ctx.path.startswith(ctx.mount_point):
                    cache_sync_path = ctx.path[len(ctx.mount_point) :].lstrip("/")
                else:
                    cache_sync_path = ctx.path.lstrip("/")

            cache_result: CacheSyncResult = backend.sync(
                path=cache_sync_path,
                mount_point=ctx.mount_point,
                include_patterns=ctx.include_patterns,
                exclude_patterns=ctx.exclude_patterns,
                generate_embeddings=ctx.generate_embeddings,
                context=ctx.context,
            )

            result.cache_synced = cache_result.files_synced
            result.cache_skipped = cache_result.files_skipped
            result.cache_bytes = cache_result.bytes_synced
            result.embeddings_generated = cache_result.embeddings_generated

            if cache_result.errors:
                for error in cache_result.errors:
                    result.errors.append(f"[cache] {error}")

            logger.info(
                f"[SYNC_MOUNT] Content cache sync complete: "
                f"synced={cache_result.files_synced}, "
                f"bytes={cache_result.bytes_synced}, "
                f"embeddings={cache_result.embeddings_generated}"
            )

        except Exception as e:
            result.errors.append(f"Failed to sync content cache: {e}")
            logger.warning(f"Failed to sync content cache: {e}")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_created_by(self, ctx: SyncContext) -> str | None:
        """Extract created_by from context.

        Args:
            ctx: SyncContext

        Returns:
            Created by string or None
        """
        if (
            ctx.context
            and hasattr(ctx.context, "subject_type")
            and hasattr(ctx.context, "subject_id")
            and ctx.context.subject_id
        ):
            subject_type = ctx.context.subject_type or "user"
            return f"{subject_type}:{ctx.context.subject_id}"
        return None

    def _get_start_paths(self, ctx: SyncContext) -> tuple[str, str]:
        """Determine starting paths for sync.

        Args:
            ctx: SyncContext

        Returns:
            Tuple of (virtual_path, backend_path)
        """
        assert ctx.mount_point is not None

        if ctx.path:
            if ctx.path.startswith(ctx.mount_point):
                start_virtual_path = ctx.path
                start_backend_path = ctx.path[len(ctx.mount_point) :].lstrip("/")
            else:
                start_virtual_path = f"{ctx.mount_point.rstrip('/')}/{ctx.path.lstrip('/')}"
                start_backend_path = ctx.path.lstrip("/")
        else:
            start_virtual_path = ctx.mount_point
            start_backend_path = ""

        return start_virtual_path, start_backend_path

    def _check_single_file(
        self,
        backend: Any,
        backend_path: str,
        context: Any,
    ) -> bool:
        """Check if path is a single file (not directory).

        Args:
            backend: Backend instance
            backend_path: Path to check
            context: Operation context

        Returns:
            True if single file, False if directory
        """
        import os.path as osp

        try:
            entries = backend.list_dir(backend_path, context=context)
            # Empty directory or file - check extension
            return bool(not entries and osp.splitext(backend_path)[1])
        except Exception:
            return True

    def _sync_single_file(
        self,
        ctx: SyncContext,
        backend: Any,
        virtual_path: str,
        backend_path: str,
        created_by: str | None,
        result: SyncResult,
        files_found: set[str],
        paths_needing_tuples: list[str],
        flush_fn: Callable[[], None],
        cached_entries: dict[str, ChangeLogEntry] | None = None,
        pending_upserts: list[ChangeLogEntry] | None = None,
    ) -> set[str]:
        """Sync a single file by delegating to _sync_file.

        Delegates to _sync_file for all file processing (including delta sync),
        then flushes parent tuples if any were queued.

        Args:
            ctx: SyncContext
            backend: Backend instance
            virtual_path: Virtual path
            backend_path: Backend path
            created_by: Creator identifier
            result: SyncResult to update
            files_found: Set to track found files
            paths_needing_tuples: List for batch tuple creation
            flush_fn: Function to flush parent tuples
            cached_entries: Pre-fetched change log entries (batch optimization)
            pending_upserts: Accumulator for batch upsert (batch optimization)

        Returns:
            Set of files found
        """
        logger.info(f"[SYNC_MOUNT] Syncing single file: {virtual_path}")

        # Check pattern match with explicit logging for single-file sync
        if not self._matches_patterns(virtual_path, ctx):
            logger.info(f"[SYNC_MOUNT] Skipping {virtual_path} - filtered by patterns")
            return files_found

        # Delegate to _sync_file for full processing (including delta sync)
        self._sync_file(
            ctx=ctx,
            backend=backend,
            virtual_path=virtual_path,
            backend_path=backend_path,
            created_by=created_by,
            result=result,
            files_found=files_found,
            paths_needing_tuples=paths_needing_tuples,
            cached_entries=cached_entries,
            pending_upserts=pending_upserts,
        )

        # Flush parent tuples immediately for single file sync
        if paths_needing_tuples:
            flush_fn()

        return files_found

    def _get_file_size(
        self,
        backend: Any,
        backend_path: str,
        _ctx: SyncContext,
    ) -> int:
        """Get file size from backend.

        Args:
            backend: Backend instance
            backend_path: Backend path
            _ctx: SyncContext (unused, kept for API consistency)

        Returns:
            File size in bytes, 0 if unavailable
        """
        try:
            if hasattr(backend, "get_content_size"):
                from nexus.core.operation_context import OperationContext

                size_context = OperationContext(backend_path=backend_path)
                # Note: content_hash is ignored by connectors - they use backend_path from context
                result: int = backend.get_content_size("", size_context).unwrap()
                return result
        except Exception:
            pass
        return 0

    def _matches_patterns(self, file_path: str, ctx: SyncContext) -> bool:
        """Check if file path matches include/exclude patterns.

        Args:
            file_path: Virtual file path
            ctx: SyncContext with patterns

        Returns:
            True if file should be included
        """
        from nexus.core import glob_fast

        # Check include patterns
        if ctx.include_patterns and not glob_fast.glob_match(file_path, list(ctx.include_patterns)):
            return False

        # Check exclude patterns
        return not (
            ctx.exclude_patterns and glob_fast.glob_match(file_path, list(ctx.exclude_patterns))
        )

    def _check_permission(
        self,
        path: str,
        permission: str,
        context: OperationContext | None,
    ) -> bool:
        """Check if user has permission on path.

        Args:
            path: Virtual path to check
            permission: Permission to check ("read", "write")
            context: Operation context

        Returns:
            True if user has permission
        """
        if not context:
            # No context = allow (backward compatibility)
            return True

        try:
            # Admin users bypass permission checks
            is_admin = getattr(context, "is_admin", False)
            if is_admin:
                return True

            subject_type, subject_id = get_user_identity(context)
            if not subject_id:
                return False

            zone_id = get_zone_id(context)

            return self._gw.rebac_check(
                subject=(subject_type, subject_id),
                permission=permission,
                object=("file", path),
                zone_id=zone_id,
            )
        except Exception as e:
            logger.error(f"Permission check failed for {path}: {e}")
            return False
