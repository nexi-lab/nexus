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

import hashlib
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.context_utils import get_tenant_id, get_user_identity

if TYPE_CHECKING:
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


@dataclass
class SyncResult:
    """Result of a sync operation."""

    files_scanned: int = 0
    files_created: int = 0
    files_updated: int = 0
    files_deleted: int = 0
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

        # Step 2: Check permission before syncing
        if not self._check_permission(ctx.mount_point, "read", ctx.context):
            raise PermissionError(f"Cannot sync mount {ctx.mount_point}: no read permission")

        result = SyncResult()

        # Step 3: Validate mount and get backend
        backend = self._validate_mount(ctx)

        # Step 4: Sync metadata (BFS traversal)
        files_found = self._sync_metadata(ctx, backend, result)

        # Step 5: Handle deletions
        self._sync_deletions(ctx, files_found, result)

        # Step 6: Sync content cache
        self._sync_content(ctx, backend, result)

        return result

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
                    tenant_id = get_tenant_id(ctx.context) if ctx.context else None
                    logger.info(
                        f"[SYNC_MOUNT] Flushing batch: creating parent tuples for "
                        f"{len(paths_needing_tuples)} files (tenant_id={tenant_id})"
                    )
                    created = self._gw.ensure_parent_tuples_batch(
                        paths_needing_tuples, tenant_id=tenant_id
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

        # Check if this is a single file sync
        if ctx.path:
            is_single_file = self._check_single_file(backend, start_backend_path, ctx.context)
            if is_single_file:
                return self._sync_single_file(
                    ctx,
                    backend,
                    start_virtual_path,
                    start_backend_path,
                    created_by,
                    result,
                    files_found,
                    paths_needing_tuples,
                    flush_parent_tuples_batch,
                )

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
                        )

                        # Flush batch if needed
                        if len(paths_needing_tuples) >= self.PATHS_CHUNK_SIZE:
                            flush_parent_tuples_batch()

            except Exception as e:
                error_msg = f"Failed to scan {virtual_path}: {e}"
                result.errors.append(error_msg)
                logger.warning(error_msg)

        # Final flush
        if paths_needing_tuples:
            flush_parent_tuples_batch()

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
    ) -> None:
        """Sync a single file entry.

        Args:
            ctx: SyncContext
            backend: Backend instance
            virtual_path: Virtual file path
            backend_path: Backend file path
            created_by: Creator identifier
            result: SyncResult to update
            files_found: Set to track found files
            paths_needing_tuples: List to collect paths for batch tuple creation
        """
        from nexus.core.metadata import FileMetadata

        # Apply pattern filtering
        if not self._matches_patterns(virtual_path, ctx):
            return

        result.files_scanned += 1

        # Call progress callback if provided
        if ctx.progress_callback:
            try:
                ctx.progress_callback(result.files_scanned, virtual_path)
            except Exception as cb_error:
                from nexus.core.sync_job_manager import SyncCancelled

                if isinstance(cb_error, SyncCancelled):
                    raise
                logger.warning(f"Progress callback error: {cb_error}")

        files_found.add(virtual_path)

        if ctx.dry_run:
            return

        # Check if file exists in metadata
        existing_meta = self._gw.metadata_get(virtual_path)

        if not existing_meta:
            try:
                # Extract tenant_id from context or mount point path
                tenant_id = get_tenant_id(ctx.context) if ctx.context else None
                if not tenant_id and ctx.mount_point:
                    match = re.match(r"^/tenant:([^/]+)/", ctx.mount_point)
                    if match:
                        tenant_id = match.group(1)

                if not tenant_id:
                    raise ValueError(
                        f"Cannot sync file {virtual_path}: tenant_id not found in context or mount point path"
                    )

                now = datetime.now(UTC)
                path_hash = hashlib.sha256(backend_path.encode()).hexdigest()

                # Get file size from backend if available
                file_size = self._get_file_size(backend, path_hash, backend_path, ctx)

                meta = FileMetadata(
                    path=virtual_path,
                    backend_name=backend.name,
                    physical_path=backend_path,
                    size=file_size,
                    etag=path_hash,
                    created_at=now,
                    modified_at=now,
                    version=1,
                    created_by=created_by,
                    tenant_id=tenant_id,
                )

                self._gw.metadata_put(meta)
                result.files_created += 1

                # Collect for batch parent tuple creation
                if self._gw.hierarchy_enabled:
                    paths_needing_tuples.append(virtual_path)

            except Exception as e:
                result.errors.append(f"Failed to add {virtual_path}: {e}")

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
        from nexus.core.metadata import FileMetadata

        files_found.add(virtual_path)

        if ctx.dry_run:
            return

        existing_meta = self._gw.metadata_get(virtual_path)
        if existing_meta:
            return

        try:
            tenant_id = get_tenant_id(ctx.context) if ctx.context else None

            # Try to extract tenant from mount point path
            if not tenant_id and ctx.mount_point:
                match = re.match(r"^/tenant:([^/]+)/", ctx.mount_point)
                if match:
                    tenant_id = match.group(1)

            if not tenant_id:
                raise ValueError(
                    f"Cannot sync directory {virtual_path}: tenant_id not found in context or mount point path"
                )

            now = datetime.now(UTC)
            path_hash = hashlib.sha256(backend_path.encode()).hexdigest()

            dir_meta = FileMetadata(
                path=virtual_path,
                backend_name=backend.name,
                physical_path=backend_path,
                size=0,
                etag=path_hash,
                mime_type="inode/directory",
                created_at=now,
                modified_at=now,
                version=1,
                created_by=created_by,
                tenant_id=tenant_id,
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
        files_found: set[str],
        result: SyncResult,
    ) -> None:
        """Handle file deletions - remove files no longer in backend.

        Only performs deletion check when syncing from root (path=None).

        Args:
            ctx: SyncContext
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

            # List all files in metadata under this mount
            existing_metas = self._gw.metadata_list(prefix=ctx.mount_point, recursive=True)
            existing_files = [meta.path for meta in existing_metas]

            for existing_path in existing_files:
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
                    meta = self._gw.metadata_get(existing_path)
                    if meta:
                        logger.info(
                            f"[SYNC_MOUNT] Deleting file no longer in backend: {existing_path}"
                        )
                        self._gw.metadata_delete(existing_path)
                        result.files_deleted += 1
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
    ) -> set[str]:
        """Sync a single file.

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

        Returns:
            Set of files found
        """
        from nexus.core.metadata import FileMetadata

        assert ctx.mount_point is not None

        if not self._matches_patterns(virtual_path, ctx):
            logger.info(f"[SYNC_MOUNT] Skipping {virtual_path} - filtered by patterns")
            return files_found

        logger.info(f"[SYNC_MOUNT] Syncing single file: {virtual_path}")
        result.files_scanned = 1
        files_found.add(virtual_path)

        if ctx.dry_run:
            return files_found

        existing_meta = self._gw.metadata_get(virtual_path)
        if not existing_meta:
            try:
                # Extract tenant_id from context or mount point path
                tenant_id = get_tenant_id(ctx.context) if ctx.context else None
                if not tenant_id and ctx.mount_point:
                    match = re.match(r"^/tenant:([^/]+)/", ctx.mount_point)
                    if match:
                        tenant_id = match.group(1)

                if not tenant_id:
                    raise ValueError(
                        f"Cannot sync file {virtual_path}: tenant_id not found in context or mount point path"
                    )

                now = datetime.now(UTC)
                path_hash = hashlib.sha256(backend_path.encode()).hexdigest()
                file_size = self._get_file_size(backend, path_hash, backend_path, ctx)

                meta = FileMetadata(
                    path=virtual_path,
                    backend_name=backend.name,
                    physical_path=backend_path,
                    size=file_size,
                    etag=path_hash,
                    created_at=now,
                    modified_at=now,
                    version=1,
                    created_by=created_by,
                    tenant_id=tenant_id,
                )
                self._gw.metadata_put(meta)
                result.files_created = 1

                if self._gw.hierarchy_enabled:
                    paths_needing_tuples.append(virtual_path)
                    flush_fn()

            except Exception as e:
                result.errors.append(f"Failed to add {virtual_path}: {e}")

        return files_found

    def _get_file_size(
        self,
        backend: Any,
        path_hash: str,
        backend_path: str,
        _ctx: SyncContext,
    ) -> int:
        """Get file size from backend.

        Args:
            backend: Backend instance
            path_hash: Hash of path
            backend_path: Backend path
            _ctx: SyncContext (unused, kept for API consistency)

        Returns:
            File size in bytes, 0 if unavailable
        """
        try:
            if hasattr(backend, "get_content_size"):
                from nexus.core.operation_context import OperationContext

                size_context = OperationContext(backend_path=backend_path)
                result: int = backend.get_content_size(path_hash, size_context)
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

            tenant_id = get_tenant_id(context)

            return self._gw.rebac_check(
                subject=(subject_type, subject_id),
                permission=permission,
                object=("file", path),
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.error(f"Permission check failed for {path}: {e}")
            return False
