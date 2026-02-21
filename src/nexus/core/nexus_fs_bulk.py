"""Bulk file operations mixin for NexusFS.

Extracted from NexusFSCoreMixin (Issue #2272, Phase 4 of LEGO plan):
- read_bulk: Batch file reads with permission batching
- write_batch: Atomic multi-file writes
- stat_bulk: Batch metadata lookups
- delete_bulk: Multi-file deletion
- metadata_batch: Batch metadata fetch
- exists_batch: Multi-path existence checks
- rename_bulk: Multi-file renames

All methods delegate single-file operations to the kernel (NexusFSCoreMixin)
and batch cross-cutting concerns (permissions, metadata lookups).
"""

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import Permission
from nexus.lib.mutation_hooks import MutationOp
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.contracts.types import OperationContext
    from nexus.contracts.write_observer import WriteObserverProtocol
    from nexus.core.metastore import MetastoreABC
    from nexus.core.router import PathRouter
    from nexus.core.vfs_hooks import VFSHookPipeline


class NexusFSBulkMixin:
    """Mixin providing bulk/batch file operations for NexusFS.

    These methods batch cross-cutting concerns (permissions, metadata lookups)
    for improved performance over N individual calls.

    Expects the host class to provide:
        - router: PathRouter
        - metadata: MetastoreABC
        - backend: Backend
        - _permission_enforcer: PermissionEnforcerProtocol | None
        - _enforce_permissions: bool
        - _default_context: OperationContext
        - _hook_pipeline: VFSHookPipeline | None
        - _write_observer: WriteObserverProtocol | None
        - _permission_checker: object with .check() method
        - auto_parse: bool
        - Various methods: _validate_path, _get_routing_params, _check_zone_writable,
          _fire_post_mutation_hooks, _increment_zone_revision,
          _check_permission, _get_zone_id, exists, delete, rename, is_directory,
          _has_descendant_access, _rmdir_internal
    """

    # Type hints for attributes provided by NexusFS parent class
    if TYPE_CHECKING:
        from nexus.core.metastore import MetastoreABC
        from nexus.core.protocols.permission_enforcer import PermissionEnforcerProtocol

        metadata: "MetastoreABC"
        backend: "Backend"
        router: "PathRouter"
        auto_parse: bool
        _default_context: "OperationContext"
        _enforce_permissions: bool
        _permission_enforcer: PermissionEnforcerProtocol | None
        _rebac_manager: Any
        _permission_checker: Any
        _write_observer: "WriteObserverProtocol | None"
        _hook_pipeline: "VFSHookPipeline | None"

        @property
        def zone_id(self) -> str | None: ...
        @property
        def agent_id(self) -> str | None: ...

        def _validate_path(self, path: str) -> str: ...
        def _check_permission(
            self,
            path: str,
            permission: Permission,
            context: "OperationContext | None",
            file_metadata: FileMetadata | None = None,
        ) -> None: ...
        def _get_routing_params(
            self, context: "OperationContext | dict[Any, Any] | None"
        ) -> tuple[str | None, str | None, bool]: ...
        def _get_created_by(
            self, context: "OperationContext | dict[Any, Any] | None"
        ) -> str | None: ...
        def _check_zone_writable(
            self, context: "OperationContext | dict | None" = None
        ) -> None: ...
        def _fire_post_mutation_hooks(
            self,
            op: MutationOp,
            path: str,
            zone_id: str,
            revision: int,
            *,
            agent_id: str | None = None,
            user_id: str | None = None,
            timestamp: str | None = None,
            etag: str | None = None,
            size: int | None = None,
            version: int | None = None,
            is_new: bool = False,
            new_path: str | None = None,
        ) -> None: ...
        def _increment_zone_revision(self) -> int: ...
        # _handle_observer_error → removed (Issue #2152)
        @staticmethod
        def _resolve_write_urgency(io_profile: str) -> str | None: ...
        def _get_zone_id(self, context: "OperationContext | None") -> str: ...
        def _has_descendant_access(
            self, path: str, permission: Permission, context: "OperationContext"
        ) -> bool: ...
        def exists(self, path: str, context: "OperationContext | None" = None) -> bool: ...
        def delete(
            self, path: str, context: "OperationContext | None" = None
        ) -> dict[str, Any]: ...
        def rename(
            self, old_path: str, new_path: str, context: "OperationContext | None" = None
        ) -> dict[str, Any]: ...
        def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool: ...
        def _rmdir_internal(
            self,
            path: str,
            recursive: bool = False,
            context: "OperationContext | None" = None,
            is_implicit: bool | None = None,
        ) -> None: ...

    # =========================================================================
    # Shared helper — DRY for bulk read result construction + hook dispatch
    # =========================================================================

    def _finalize_bulk_read(
        self,
        path: str,
        content: bytes,
        meta: FileMetadata,
        context: Any,
        return_metadata: bool,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> bytes | dict[str, Any]:
        """Apply post-read hooks and build result for a single file in bulk read.

        Replaces the repeated inline filter + result dict pattern.
        Hooks may transform content (e.g., dynamic viewer CSV filtering).
        """
        _pipeline = getattr(self, "_hook_pipeline", None)
        if _pipeline is not None and _pipeline.read_hook_count > 0:
            from nexus.core.vfs_hooks import ReadHookContext

            _read_ctx = ReadHookContext(
                path=path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                metadata=meta,
                content=content,
                content_hash=meta.etag,
            )
            _pipeline.run_post_read(_read_ctx)
            # Issue #2272: use `is not None` to avoid treating empty bytes as falsy
            content = _read_ctx.content if _read_ctx.content is not None else content

        if return_metadata:
            return {
                "content": content,
                "etag": meta.etag,
                "version": meta.version,
                "modified_at": meta.modified_at,
                "size": len(content),
            }
        return content

    # =========================================================================
    # read_bulk
    # =========================================================================

    @rpc_expose(description="Read multiple files in a single RPC call")
    def read_bulk(
        self,
        paths: list[str],
        context: "OperationContext | None" = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """Read multiple files in a single RPC call for improved performance.

        This method is optimized for bulk operations like grep, where many files
        need to be read. It batches permission checks and reduces RPC overhead.

        Args:
            paths: List of virtual paths to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dicts with content and metadata
            skip_errors: If True, skip files that can't be read and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> content (or None if skip_errors=True and read failed)
            If return_metadata=False: {path: bytes}
            If return_metadata=True: {path: {content, etag, version, ...}}

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Reduced network round trips
            - Expected speedup: 2-5x for 50+ files

        Examples:
            >>> results = nx.read_bulk(["/file1.txt", "/file2.txt", "/file3.txt"])
            >>> print(results["/file1.txt"])  # b'content'

            >>> results = nx.read_bulk(["/file1.txt"], return_metadata=True)
            >>> print(results["/file1.txt"]["content"])
        """
        bulk_start = time.time()
        results: dict[str, bytes | dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in read_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check using filter_list
        perm_start = time.time()
        allowed_set: set[str]
        if not self._enforce_permissions:
            # Skip permission check if permissions are disabled
            allowed_set = set(validated_paths)
        else:
            try:
                from nexus.contracts.types import OperationContext

                ctx = context if context is not None else self._default_context
                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                assert self._permission_enforcer is not None  # guarded by _enforce_permissions
                allowed_paths = self._permission_enforcer.filter_list(validated_paths, ctx)
                allowed_set = set(allowed_paths)
            except Exception as e:
                logger.error(f"[READ-BULK] Permission check failed: {e}")
                if not skip_errors:
                    raise
                # If skip_errors, assume no files are allowed
                allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[READ-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} "
            f"allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Read allowed files
        read_start = time.time()
        zone_id, agent_id, is_admin = self._get_routing_params(context)

        # Group paths by backend for potential bulk optimization
        path_info: dict[str, tuple[FileMetadata, Any]] = {}  # path -> (meta, route)
        backend_paths: dict[Any, list[str]] = {}  # backend -> [paths]

        # Batch metadata lookup
        meta_start = time.time()
        batch_meta = self.metadata.get_batch(list(allowed_set))
        meta_elapsed = (time.time() - meta_start) * 1000
        logger.info(
            f"[READ-BULK] Batch metadata lookup: {len(batch_meta)} paths in {meta_elapsed:.1f}ms"
        )

        # Process metadata and group by backend
        route_start = time.time()
        for path in allowed_set:
            try:
                meta = batch_meta.get(path)
                if meta is None or meta.etag is None:
                    if skip_errors:
                        results[path] = None
                        continue
                    raise NexusFileNotFoundError(path)

                route = self.router.route(
                    path,
                    zone_id=zone_id,
                    is_admin=is_admin,
                    check_write=False,
                )
                path_info[path] = (meta, route)

                # Group by backend
                backend = route.backend
                if backend not in backend_paths:
                    backend_paths[backend] = []
                backend_paths[backend].append(path)
            except Exception as e:
                logger.warning(f"[READ-BULK] Failed to route {path}: {type(e).__name__}: {e}")
                if skip_errors:
                    results[path] = None
                else:
                    raise

        route_elapsed = (time.time() - route_start) * 1000
        logger.info(f"[READ-BULK] Routing: {len(path_info)} paths in {route_elapsed:.1f}ms")

        # Try bulk read for backends that support it (CacheConnectorMixin)
        for backend, paths_for_backend in backend_paths.items():
            if hasattr(backend, "read_bulk_from_cache") and len(paths_for_backend) > 1:
                self._read_bulk_via_cache(
                    backend,
                    paths_for_backend,
                    path_info,
                    results,
                    context,
                    return_metadata,
                    skip_errors,
                    zone_id,
                    agent_id,
                )
            elif backend.supports_parallel_mmap_read is True and len(paths_for_backend) > 1:
                self._read_bulk_via_mmap(
                    backend,
                    paths_for_backend,
                    path_info,
                    results,
                    context,
                    return_metadata,
                    skip_errors,
                    zone_id,
                    agent_id,
                )
            else:
                self._read_bulk_sequential(
                    paths_for_backend,
                    path_info,
                    results,
                    context,
                    return_metadata,
                    skip_errors,
                    zone_id,
                    agent_id,
                )

        read_elapsed = time.time() - read_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[READ-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, read={read_elapsed * 1000:.0f}ms)"
        )

        return results

    # --- read_bulk internal strategies (extracted for DRY) ---

    def _read_bulk_via_cache(
        self,
        backend: Any,
        paths_for_backend: list[str],
        path_info: dict[str, tuple[FileMetadata, Any]],
        results: dict[str, bytes | dict[str, Any] | None],
        context: Any,
        return_metadata: bool,
        skip_errors: bool,
        zone_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Bulk read using backend cache (CacheConnectorMixin)."""
        logger.info(
            f"[READ-BULK] Using bulk cache for {len(paths_for_backend)} files "
            f"on {type(backend).__name__}"
        )
        try:
            cache_entries = backend.read_bulk_from_cache(paths_for_backend, original=True)

            paths_needing_backend: list[str] = []
            for path in paths_for_backend:
                entry = cache_entries.get(path)
                if entry and not entry.stale and entry.content_binary:
                    meta, _route = path_info[path]
                    results[path] = self._finalize_bulk_read(
                        path,
                        entry.content_binary,
                        meta,
                        context,
                        return_metadata,
                        zone_id,
                        agent_id,
                    )
                else:
                    paths_needing_backend.append(path)

            # Fall back to individual reads for cache misses
            self._read_bulk_sequential(
                paths_needing_backend,
                path_info,
                results,
                context,
                return_metadata,
                skip_errors,
                zone_id,
                agent_id,
            )
        except Exception as e:
            logger.warning(f"[READ-BULK] Bulk cache failed, falling back to individual reads: {e}")
            self._read_bulk_sequential(
                paths_for_backend,
                path_info,
                results,
                context,
                return_metadata,
                skip_errors,
                zone_id,
                agent_id,
            )

    def _read_bulk_via_mmap(
        self,
        backend: Any,
        paths_for_backend: list[str],
        path_info: dict[str, tuple[FileMetadata, Any]],
        results: dict[str, bytes | dict[str, Any] | None],
        context: Any,
        return_metadata: bool,
        skip_errors: bool,
        zone_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Bulk read using Rust parallel mmap for LocalBackend."""
        try:
            from nexus_fast import read_files_bulk

            disk_to_virtual: dict[str, tuple[str, Any]] = {}
            disk_paths: list[str] = []
            for path in paths_for_backend:
                meta, _route = path_info[path]
                assert meta.etag is not None
                disk_path = str(backend._hash_to_path(meta.etag))
                disk_to_virtual[disk_path] = (path, meta)
                disk_paths.append(disk_path)

            logger.info(f"[READ-BULK] Using parallel mmap for {len(disk_paths)} LocalBackend files")
            disk_contents = read_files_bulk(disk_paths)

            for disk_path, content in disk_contents.items():
                vpath, meta = disk_to_virtual[disk_path]
                results[vpath] = self._finalize_bulk_read(
                    vpath,
                    content,
                    meta,
                    context,
                    return_metadata,
                    zone_id,
                    agent_id,
                )

            # Mark missing files
            for path in paths_for_backend:
                if path not in results:
                    if skip_errors:
                        results[path] = None
                    else:
                        raise NexusFileNotFoundError(path)
        except ImportError:
            logger.warning("[READ-BULK] nexus_fast not available, falling back to sequential")
            self._read_bulk_sequential(
                [p for p in paths_for_backend if p not in results],
                path_info,
                results,
                context,
                return_metadata,
                skip_errors,
                zone_id,
                agent_id,
            )

    def _read_bulk_sequential(
        self,
        paths_for_backend: list[str],
        path_info: dict[str, tuple[FileMetadata, Any]],
        results: dict[str, bytes | dict[str, Any] | None],
        context: Any,
        return_metadata: bool,
        skip_errors: bool,
        zone_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Sequential reads for individual files."""
        for path in paths_for_backend:
            if path in results:
                continue
            try:
                meta, route = path_info[path]
                assert meta.etag is not None
                read_context = context
                if context:
                    from dataclasses import replace

                    read_context = replace(context, backend_path=route.backend_path)
                content = route.backend.read_content(meta.etag, context=read_context).unwrap()
                results[path] = self._finalize_bulk_read(
                    path,
                    content,
                    meta,
                    context,
                    return_metadata,
                    zone_id,
                    agent_id,
                )
            except Exception as e:
                logger.warning(f"[READ-BULK] Failed to read {path}: {type(e).__name__}: {e}")
                if skip_errors:
                    results[path] = None
                else:
                    raise

    # =========================================================================
    # write_batch
    # =========================================================================

    @rpc_expose(description="Write multiple files in a single transaction")
    def write_batch(
        self, files: list[tuple[str, bytes]], context: "OperationContext | None" = None
    ) -> list[dict[str, Any]]:
        """Write multiple files in a single transaction for improved performance.

        This is 13x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        All files are written atomically - either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks

        Returns:
            List of metadata dicts for each file (in same order as input)

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied
            PermissionError: If any path is read-only
        """
        if not files:
            return []

        self._check_zone_writable(context)  # Issue #2061: write-gating

        # Validate all paths first
        validated_files: list[tuple[str, bytes]] = []
        for path, content in files:
            validated_path = self._validate_path(path)
            validated_files.append((validated_path, content))

        # Route all paths and check write access
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        routes = []
        for path, _ in validated_files:
            route = self.router.route(
                path,
                zone_id=zone_id,
                is_admin=is_admin,
                check_write=True,
            )
            if route.readonly:
                raise PermissionError(f"Path is read-only: {path}")
            routes.append(route)

        # Get existing metadata for all paths (single query)
        paths = [path for path, _ in validated_files]
        existing_metadata = self.metadata.get_batch(paths)

        # Check write permissions for existing files
        if self._enforce_permissions:
            for path in paths:
                meta = existing_metadata.get(path)
                if meta is not None:
                    self._permission_checker.check(
                        path, Permission.WRITE, context, file_metadata=meta
                    )

        now = datetime.now(UTC)
        metadata_list: list[FileMetadata] = []
        results: list[dict[str, Any]] = []

        # Write all content to backend CAS (deduplicated automatically)
        for (path, content), route in zip(validated_files, routes, strict=False):
            content_hash = route.backend.write_content(content, context=context).unwrap()

            meta = existing_metadata.get(path)
            new_version = (meta.version + 1) if meta else 1

            metadata = FileMetadata(
                path=path,
                backend_name=route.backend.name,
                physical_path=content_hash,
                size=len(content),
                etag=content_hash,
                created_at=meta.created_at if meta else now,
                modified_at=now,
                version=new_version,
                created_by=getattr(self, "agent_id", None) or getattr(self, "user_id", None),
                zone_id=zone_id or "root",
            )
            metadata_list.append(metadata)

            results.append(
                {
                    "etag": content_hash,
                    "version": new_version,
                    "modified_at": now,
                    "size": len(content),
                }
            )

        # Store all metadata in a single transaction
        self.metadata.put_batch(metadata_list)

        # Sync batch to RecordStore (audit trail + version history)
        # Observer owns error policy (Issue #2152).
        items = [
            (metadata, existing_metadata.get(metadata.path) is None) for metadata in metadata_list
        ]
        if (obs := self._write_observer) is not None:
            # Resolve urgency from the first route's IOProfile (#2426 Phase 2).
            # Batch writes share the same mount, so all routes have the same io_profile.
            _urgency = self._resolve_write_urgency(routes[0].io_profile) if routes else None
            # observer owns error policy (#2152)
            obs.on_write_batch(
                items=items,
                zone_id=zone_id,
                agent_id=agent_id,
                urgency=_urgency,
            )

        # Fire post-mutation hooks for each file in the batch
        new_revision = self._increment_zone_revision()
        for metadata in metadata_list:
            is_new = existing_metadata.get(metadata.path) is None
            self._fire_post_mutation_hooks(
                MutationOp.WRITE,
                metadata.path,
                zone_id or ROOT_ZONE_ID,
                new_revision,
                agent_id=agent_id,
                etag=metadata.etag,
                size=metadata.size,
                version=metadata.version,
                is_new=is_new,
            )

        # Issue #2175: Publish batch events to EventBus using publish_batch()
        # for single fire-and-forget crossing (Redis pipeline = single RTT).
        _event_bus = getattr(self, "_event_bus", None)
        if _event_bus is not None and metadata_list:
            try:
                from nexus.core.file_events import FileEvent, FileEventType
                from nexus.lib.sync_bridge import fire_and_forget

                batch_events = [
                    FileEvent(
                        type=FileEventType.FILE_WRITE,
                        path=meta.path,
                        zone_id=zone_id or ROOT_ZONE_ID,
                        size=meta.size,
                        etag=meta.etag,
                        agent_id=agent_id,
                        revision=new_revision,
                    )
                    for meta in metadata_list
                ]

                # Single async dispatch for entire batch
                if not getattr(_event_bus, "_started", False):

                    async def _start_and_batch() -> None:
                        await _event_bus.start()
                        await _event_bus.publish_batch(batch_events)

                    fire_and_forget(_start_and_batch())
                else:
                    fire_and_forget(_event_bus.publish_batch(batch_events))
            except Exception as e:
                logger.warning(
                    "write_batch: Failed to publish %d events: %s", len(metadata_list), e
                )

        # Create parent tuples and grant direct_owner for new files
        ctx = context if context is not None else self._default_context
        zone_id_for_perms = ctx.zone_id or "root"

        # Batch hierarchy tuple creation
        _hierarchy_start = time.perf_counter()
        all_paths = [path for path, _ in validated_files]
        if hasattr(self, "_hierarchy_manager") and hasattr(
            self._hierarchy_manager, "ensure_parent_tuples_batch"
        ):
            try:
                created_count = self._hierarchy_manager.ensure_parent_tuples_batch(
                    all_paths, zone_id=zone_id_for_perms
                )
                logger.info(
                    f"write_batch: Batch created {created_count} parent tuples "
                    f"for {len(all_paths)} files"
                )
            except Exception as e:
                logger.warning(f"write_batch: Batch parent tuples failed, falling back: {e}")
                for path in all_paths:
                    try:
                        self._hierarchy_manager.ensure_parent_tuples(
                            path, zone_id=zone_id_for_perms
                        )
                    except Exception as e2:
                        logger.warning(f"write_batch: Failed parent tuples for {path}: {e2}")
        elif hasattr(self, "_hierarchy_manager"):
            for path in all_paths:
                try:
                    self._hierarchy_manager.ensure_parent_tuples(path, zone_id=zone_id_for_perms)
                except Exception as e:
                    logger.warning(f"write_batch: Failed parent tuples for {path}: {e}")
        _hierarchy_elapsed = (time.perf_counter() - _hierarchy_start) * 1000

        # Batch direct_owner grants
        _rebac_start = time.perf_counter()
        if self._rebac_manager and ctx.user_id and not ctx.is_system:
            owner_grants = []
            for (path, _), _meta in zip(validated_files, metadata_list, strict=False):
                is_new_file = existing_metadata.get(path) is None
                if is_new_file:
                    owner_grants.append(
                        {
                            "subject": ("user", ctx.user_id),
                            "relation": "direct_owner",
                            "object": ("file", path),
                            "zone_id": zone_id_for_perms,
                        }
                    )

            if owner_grants and hasattr(self._rebac_manager, "rebac_write_batch"):
                try:
                    grant_count = self._rebac_manager.rebac_write_batch(owner_grants)
                    logger.info(f"write_batch: Batch granted direct_owner to {grant_count} files")
                except Exception as e:
                    logger.warning(f"write_batch: Batch rebac_write failed, falling back: {e}")
                    for grant in owner_grants:
                        try:
                            self._rebac_manager.rebac_write(
                                subject=grant["subject"],
                                relation=grant["relation"],
                                object=grant["object"],
                                zone_id=grant["zone_id"],
                            )
                        except Exception as e2:
                            logger.warning(f"write_batch: Failed direct_owner grant: {e2}")
            elif owner_grants:
                for grant in owner_grants:
                    try:
                        self._rebac_manager.rebac_write(
                            subject=grant["subject"],
                            relation=grant["relation"],
                            object=grant["object"],
                            zone_id=grant["zone_id"],
                        )
                    except Exception as e:
                        logger.warning(f"write_batch: Failed direct_owner grant: {e}")
        _rebac_elapsed = (time.perf_counter() - _rebac_start) * 1000

        # Issue #2272: Changed from logger.warning to logger.info
        logger.info(
            f"[WRITE-BATCH-PERF] files={len(validated_files)}, "
            f"hierarchy={_hierarchy_elapsed:.1f}ms, rebac={_rebac_elapsed:.1f}ms, "
            f"per_file_avg={(_hierarchy_elapsed + _rebac_elapsed) / len(validated_files):.1f}ms"
        )

        # Dispatch post-write hooks (auto-parse, etc.)
        _pipeline = getattr(self, "_hook_pipeline", None)
        if _pipeline is not None and _pipeline.write_hook_count > 0:
            from nexus.core.vfs_hooks import WriteHookContext

            for (path, content), file_meta in zip(validated_files, metadata_list, strict=False):
                _write_ctx = WriteHookContext(
                    path=path,
                    content=content,
                    context=context,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    is_new_file=(existing_metadata.get(path) is None),
                    content_hash=file_meta.etag,
                    old_metadata=existing_metadata.get(path),
                    new_version=file_meta.version,
                )
                _pipeline.run_post_write(_write_ctx)

        return results

    # =========================================================================
    # stat_bulk
    # =========================================================================

    @rpc_expose(description="Get metadata for multiple files in bulk")
    def stat_bulk(
        self,
        paths: list[str],
        context: "OperationContext | None" = None,
        skip_errors: bool = True,
    ) -> dict[str, dict[str, Any] | None]:
        """Get metadata for multiple files in a single RPC call.

        Optimized with batch permission checks and metadata lookups.

        Args:
            paths: List of virtual paths to stat
            context: Optional operation context for permission checks
            skip_errors: If True, skip files that can't be stat'd and return None.

        Returns:
            Dict mapping path -> stat dict (or None)

        Performance:
            Expected speedup: 10-50x for 100+ files
        """
        bulk_start = time.time()
        results: dict[str, dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in stat_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check
        perm_start = time.time()
        allowed_set: set[str]
        if not self._enforce_permissions:
            allowed_set = set(validated_paths)
        else:
            try:
                from nexus.contracts.types import OperationContext

                ctx = context if context is not None else self._default_context
                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                assert self._permission_enforcer is not None  # guarded by _enforce_permissions
                allowed_paths = self._permission_enforcer.filter_list(validated_paths, ctx)
                allowed_set = set(allowed_paths)
            except Exception as e:
                logger.error(f"[STAT-BULK] Permission check failed: {e}")
                if not skip_errors:
                    raise
                allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[STAT-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} "
            f"allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Batch metadata lookup
        meta_start = time.time()
        try:
            batch_meta = self.metadata.get_batch(list(allowed_set))
            for path, meta in batch_meta.items():
                if meta is None:
                    _is_implicit = getattr(self.metadata, "is_implicit_directory", None)
                    if _is_implicit is not None and _is_implicit(path):
                        results[path] = {
                            "size": 0,
                            "etag": None,
                            "version": None,
                            "modified_at": None,
                            "is_directory": True,
                        }
                    elif skip_errors:
                        results[path] = None
                    else:
                        raise NexusFileNotFoundError(path)
                else:
                    modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None
                    results[path] = {
                        "size": meta.size,
                        "etag": meta.etag,
                        "version": meta.version,
                        "modified_at": modified_at_str,
                        "is_directory": False,
                    }
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            logger.warning(f"[STAT-BULK] Batch metadata failed: {type(e).__name__}: {e}")
            if not skip_errors:
                raise

        meta_elapsed = time.time() - meta_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[STAT-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, meta={meta_elapsed * 1000:.0f}ms)"
        )

        return results

    # =========================================================================
    # delete_bulk
    # =========================================================================

    @rpc_expose(description="Delete multiple files/directories")
    def delete_bulk(
        self,
        paths: list[str],
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> dict[str, dict]:
        """Delete multiple files or directories in a single operation.

        Each path is processed independently - failures on one path don't affect others.

        Args:
            paths: List of virtual paths to delete
            recursive: If True, delete non-empty directories
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each path to its result:
                {"success": True} or {"success": False, "error": "error message"}
        """
        self._check_zone_writable(context)  # Issue #2061: write-gating
        results = {}
        for path in paths:
            try:
                path = self._validate_path(path)
                meta = self.metadata.get(path)

                _is_implicit = getattr(self.metadata, "is_implicit_directory", None)
                is_implicit_dir = meta is None and _is_implicit is not None and _is_implicit(path)

                if meta is None and not is_implicit_dir:
                    results[path] = {"success": False, "error": "File not found"}
                    continue

                is_dir = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                if is_dir:
                    self._rmdir_internal(
                        path,
                        recursive=recursive,
                        context=context,
                        is_implicit=is_implicit_dir,
                    )
                else:
                    self.delete(path, context=context)

                results[path] = {"success": True}
            except Exception as e:
                results[path] = {"success": False, "error": str(e)}

        return results

    # =========================================================================
    # exists_batch
    # =========================================================================

    @rpc_expose(description="Check existence of multiple paths in single call")
    def exists_batch(
        self, paths: list[str], context: "OperationContext | None" = None
    ) -> dict[str, bool]:
        """Check existence of multiple paths in a single call (Issue #859).

        Args:
            paths: List of virtual paths to check
            context: Operation context for permission checks

        Returns:
            Dictionary mapping each path to its existence status (True/False)
        """
        results: dict[str, bool] = {}
        for path in paths:
            try:
                results[path] = self.exists(path, context=context)
            except Exception as exc:
                logger.debug("Exists check failed for %s: %s", path, exc)
                results[path] = False
        return results

    # =========================================================================
    # metadata_batch
    # =========================================================================

    @rpc_expose(description="Get metadata for multiple paths in single call")
    def metadata_batch(
        self, paths: list[str], context: "OperationContext | None" = None
    ) -> dict[str, dict[str, Any] | None]:
        """Get metadata for multiple paths in a single call (Issue #859).

        Args:
            paths: List of virtual paths to get metadata for
            context: Operation context for permission checks

        Returns:
            Dictionary mapping each path to its metadata dict or None if not found.
        """
        results: dict[str, dict[str, Any] | None] = {}

        # Validate paths and collect valid ones
        valid_paths: list[str] = []
        for path in paths:
            try:
                validated = self._validate_path(path)
                valid_paths.append(validated)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_batch for %s: %s", path, exc)
                results[path] = None

        # Batch fetch metadata from database
        if valid_paths and hasattr(self.metadata, "get_batch"):
            batch_metadata = self.metadata.get_batch(valid_paths)
        else:
            batch_metadata = {p: self.metadata.get(p) for p in valid_paths}

        # Process results with permission checks
        for path in valid_paths:
            try:
                meta = batch_metadata.get(path)

                if meta is None:
                    results[path] = None
                    continue

                # Check permission if enforcement enabled
                if self._enforce_permissions:
                    ctx = context if context is not None else self._default_context
                    if not self._has_descendant_access(path, Permission.READ, ctx):
                        results[path] = None
                        continue

                is_dir = self.is_directory(path, context=context)
                results[path] = {
                    "path": meta.path,
                    "backend_name": meta.backend_name,
                    "physical_path": meta.physical_path,
                    "size": meta.size,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "created_at": meta.created_at,
                    "modified_at": meta.modified_at,
                    "version": meta.version,
                    "zone_id": meta.zone_id,
                    "is_directory": is_dir,
                }
            except Exception as exc:
                logger.debug("Failed to build metadata result for %s: %s", path, exc)
                results[path] = None

        return results

    # =========================================================================
    # rename_bulk
    # =========================================================================

    @rpc_expose(description="Rename/move multiple files")
    def rename_bulk(
        self,
        renames: list[tuple[str, str]],
        context: "OperationContext | None" = None,
    ) -> dict[str, dict]:
        """Rename/move multiple files in a single operation.

        Each rename is processed independently - failures on one don't affect others.

        Args:
            renames: List of (old_path, new_path) tuples
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each old_path to its result:
                {"success": True, "new_path": "..."} or {"success": False, "error": "..."}
        """
        results = {}
        for old_path, new_path in renames:
            try:
                self.rename(old_path, new_path, context=context)
                results[old_path] = {"success": True, "new_path": new_path}
            except Exception as e:
                results[old_path] = {"success": False, "error": str(e)}

        return results
