"""Workspace listing, glob and grep, plus indexed queries through the search plugin."""

import asyncio
import builtins
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, cast

from cachetools import TTLCache

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import PermissionDeniedError
from nexus.contracts.protocols.activity import EventKind, Result, emit
from nexus.contracts.rebac_types import is_strong_consistency
from nexus.contracts.types import Permission
from nexus.lib.rpc_decorator import rpc_expose
from nexus.lib.zone_visibility import audit_all_zones, resolve_zone_view

# List directory traversal thresholds (Issue #901)
# Issue #2071: LIST_PARALLEL_WORKERS now sourced from ProfileTuning.search.list_parallel_workers
# Kept as fallback for callers that don't receive tuning via DI.
LIST_PARALLEL_WORKERS = 10  # Thread pool size for parallel directory listing (FULL profile default)
LIST_PARALLEL_MAX_DEPTH = 100  # Safety limit to prevent infinite traversal (e.g., symlink loops)

# Directory-like entry_type values in the sys_readdir detail dict
# (DT_DIR=1, DT_MOUNT=5). The list pipeline treats both as "directory".
_DIR_ENTRY_TYPES: frozenset[int] = frozenset({1, 5})


def _entry_is_dir(entry: dict[str, Any]) -> bool:
    """True when a sys_readdir detail dict represents a directory or mount."""
    return entry.get("entry_type") in _DIR_ENTRY_TYPES


# Zone-aware path prefixes for cross-zone filtering (Issue #899)
ZONE_AWARE_PREFIXES: tuple[str, ...] = ("/zones/", "/shared/", "/archives/")

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.bricks.rebac.enforcer import PermissionEnforcer
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS


class SearchService:
    """Workspace listing and clients for discovery and indexed search."""

    def __init__(
        self,
        metadata_store: "Any",
        permission_enforcer: "PermissionEnforcer | None" = None,
        dlc: Any = None,
        rebac_manager: "ReBACManager | None" = None,
        enforce_permissions: bool = True,
        default_context: "OperationContext | None" = None,
        record_store: Any | None = None,
        # Direct NexusFS access (replaces NexusFSGateway, Issue #1287)
        nexus_fs: "NexusFS | None" = None,
        list_parallel_workers: int = LIST_PARALLEL_WORKERS,
    ):
        """Initialize search service.

        Args:
            metadata_store: Metadata store for file information
            permission_enforcer: Permission enforcer for access control
            dlc: DriverLifecycleCoordinator for routing + backend refs
            rebac_manager: ReBAC manager for relationship-based permissions
            enforce_permissions: Whether to enforce permission checks
            default_context: Default operation context (embedded mode)
            record_store: RecordStoreABC for file-path metadata
            nexus_fs: NexusFS instance for file ops, routing, and dependency tracking
        """
        self.metadata = metadata_store
        # The owning Kernel client serves listing syscalls and discovery RPCs.
        self._kernel = metadata_store
        self._record_store = record_store
        self._fp_engine: Any = None  # Issue #3266: cached SQLAlchemy engine
        self._permission_enforcer = permission_enforcer
        self._dlc = dlc
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._default_context = default_context

        # Direct NexusFS access (replaces NexusFSGateway, Issue #1287)
        self._nexus_fs = nexus_fs

        # Shared thread pool for parallel directory listing (Issue #899)
        self._list_thread_pool: ThreadPoolExecutor | None = None
        self._list_parallel_workers = list_parallel_workers

        # Lock for lazy thread pool initialization (prevents TOCTOU race)
        self._pool_lock = threading.Lock()

        # Bounded TTL cache for cross-zone sharing queries (Issue #904)
        self._cross_zone_cache: TTLCache[tuple[str, ...], builtins.list[str]] = TTLCache(
            maxsize=1024, ttl=5.0
        )

        logger.info("[SearchService] Initialized")

    def _get_list_thread_pool(self) -> ThreadPoolExecutor:
        """Get or create the shared thread pool for parallel directory listing."""
        if self._list_thread_pool is None:
            with self._pool_lock:
                if self._list_thread_pool is None:
                    self._list_thread_pool = ThreadPoolExecutor(
                        max_workers=self._list_parallel_workers,
                        thread_name_prefix="nexus-list",
                    )
        return self._list_thread_pool

    def close(self) -> None:
        """Release resources held by the search service."""
        if self._list_thread_pool is not None:
            self._list_thread_pool.shutdown(wait=False)
            self._list_thread_pool = None

    # =========================================================================
    # Delegation Helpers (direct NexusFS access, Issue #1287)
    # =========================================================================

    def _get_routing_params(self, context: Any) -> tuple[str | None, str | None, bool]:
        """Extract zone_id, agent_id, is_admin from context."""
        if self._nexus_fs:
            return self._nexus_fs._get_context_identity(context)
        return None, None, False

    def _has_descendant_access(self, path: str, permission: Permission, context: Any) -> bool:
        """Check if user has access to any descendant of path."""
        if self._nexus_fs:
            return self._nexus_fs._descendant_checker.has_access(path, permission, context)
        return False

    def _get_backend_directory_entries(self, path: str) -> set[str]:  # noqa: ARG002
        """Backend directory entries — removed, metastore is authoritative."""
        return set()

    def _record_read_if_tracking(
        self,
        context: Any,
        resource_type: str,
        resource_id: str,
        access_type: str = "content",
    ) -> None:
        """Record read for dependency tracking (Issue #1166)."""
        if context and getattr(context, "track_reads", False):
            kernel = getattr(self._nexus_fs, "_kernel", None) if self._nexus_fs else None
            zone_id = getattr(context, "zone_id", None) or "root"
            revision = kernel.get_zone_revision(zone_id) if kernel else 0
            context.record_read(resource_type, resource_id, revision, access_type)

    # =========================================================================
    # Public API: File Listing
    # =========================================================================

    @rpc_expose(description="List files in directory")
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,  # noqa: ARG002
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
        all_zones: bool = False,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any:
        """List files in a directory.

        Supports memory virtual paths, cursor-based pagination (Issue #937),
        dynamic API-backed connectors, and ReBAC permission filtering.

        Args:
            path: Directory path to list (default: "/", supports memory paths)
            recursive: If True, list all files recursively (default: True)
            details: If True, return detailed metadata dicts (default: False)
            show_parsed: If True, include parsed virtual views (default: True)
            context: Operation context for permission filtering
            limit: Max items per page (enables pagination mode)
            cursor: Continuation token from previous page
            all_zones: Admin only — enumerate across every zone instead of
                the caller's zone; audited (#4740). Non-admins get
                PermissionDeniedError.
        """
        # Issue #4740: resolve the caller's zone view first and fail closed —
        # a zone-less non-admin caller is refused instead of receiving the
        # root/global view, and admins only cross zones with an explicit,
        # audited ``all_zones=True``.  The service's default context is the
        # kernel's own init credential (embedded operator): passed explicitly
        # it keeps the unrestricted view ``context=None`` would get.
        _zone_view = resolve_zone_view(
            context,
            all_zones=all_zones,
            operation="search.list",
            init_cred=self._default_context,
        )
        if _zone_view.all_zones:
            audit_all_zones(context, operation="search.list", path=path)

        # Issue #937: Pagination mode
        if limit is not None:
            return self._list_paginated(
                path=path,
                recursive=recursive,
                details=details,
                limit=limit,
                cursor=cursor,
                context=context,
                zone_view=_zone_view,
            )
        # Check if path routes to a dynamic API-backed connector.
        # Detect via mount root metadata is_external_storage flag (§12d).
        if path and path != "/":
            try:
                zone_id, _agent_id, _is_admin = self._get_routing_params(context)
                # Derive mount point from first path segments
                _parts = path.strip("/").split("/")
                _mp_guess = "/" + "/".join(_parts[:2]) if len(_parts) >= 2 else "/" + _parts[0]
                from nexus.contracts.types import OperationContext as _OC

                _mount_stat = (
                    self._nexus_fs.sys_stat(
                        _mp_guess,
                        context=_OC(user_id="system", groups=[], is_system=True),
                    )
                    if self._nexus_fs
                    else None
                )
                _is_ext = _mount_stat is not None and _mount_stat.get("entry_type") == 5
                if _is_ext:
                    _bp = path[len(_mp_guess) :].lstrip("/")

                    class _ExtRoute:
                        def __init__(
                            self, backend: Any, backend_path: str, mount_point: str
                        ) -> None:
                            self.backend = backend
                            self.backend_path = backend_path
                            self.mount_point = mount_point

                    ext_route = _ExtRoute(None, _bp, _mp_guess)
                    return self._list_dynamic_connector(
                        path, ext_route, recursive, details, context
                    )
            except PermissionDeniedError:
                raise
            except Exception as e:
                import traceback

                logger.debug(
                    f"Dynamic connector list_dir failed for {path}: {e}\n{traceback.format_exc()}"
                )

        # Issue #904: Extract zone_id for PREWHERE-style DB filtering
        list_zone_id, subject_type, subject_id = self._extract_zone_info(context)

        import time as _time

        _list_start = _time.time()
        _preapproved_dirs: set[str] = set()
        _accessible_int_ids: set[int] | None = None

        if path and path != "/":
            path = self._validate_path(path)
        if path and not path.endswith("/"):
            path = path + "/"
        list_prefix = path if path != "/" else ""

        # #4740: the scan runs on the path exactly as scoped by the caller's
        # layer.  An earlier "#3779 follow-up" stripped the caller's
        # ``/zone/<id>`` prefix here on the assumption that standalone rows
        # were stored flat with a zone column; the kernel stores them under
        # ``/zone/<id>/…`` (verified against the pinned kernel), so that
        # strip made every zone-scoped list/glob/grep scan the ROOT namespace
        # and return nothing for tenants whenever permissions were enforced.

        # OPTIMIZATION: For non-recursive, try sparse directory index + Tiger bitmap
        _use_fast_path = False
        _revision_before: int | None = None
        _rebac_manager = (
            getattr(self._permission_enforcer, "rebac_manager", None)
            if self._permission_enforcer
            else None
        )

        logger.info(
            f"[LIST-DEBUG] START path={path}, recursive={recursive}, zone={list_zone_id}, "
            f"details={details}, has_context={context is not None}"
        )
        # ``list_directory_entries`` was a Raft-side sparse-index helper that
        # the kernel doesn't expose; the fast path is permanently disabled.
        # The block below is kept (gated by a constant ``False``) so a
        # future kernel-side equivalent can re-enable it without
        # restructuring the caller.
        _LIST_FAST_PATH_AVAILABLE = False
        if _LIST_FAST_PATH_AVAILABLE and not recursive and not details and context:
            all_files, _preapproved_dirs, _use_fast_path, _revision_before = self._list_fast_path(
                path, list_zone_id, context, _rebac_manager
            )

        if not _use_fast_path:
            all_files, _accessible_int_ids = self._list_slow_path(
                list_prefix,
                list_zone_id,
                subject_type,
                subject_id,
                _revision_before,
                _rebac_manager,
                # Issue #4739: strong consistency must not pre-filter by the
                # Tiger bitmap; fall through to filter_list (strong chain).
                use_tiger_pushdown=not is_strong_consistency(getattr(context, "consistency", None)),
            )
            sample_paths = [m["path"] for m in all_files[:5]]
            logger.info(f"[LIST-DEBUG] FALLBACK all_files sample: {sample_paths}")

        # Issue #3779 follow-up / #4740: the metastore is shared across zones
        # (each row carries a zone_id), so the zone predicate is applied to
        # the candidate set here.  The predicate is the caller's ZoneView:
        # root-tagged rows are visible only to callers that can read the
        # root zone, and admins no longer skip the filter unless they asked
        # for ``all_zones``.  Cross-zone shared files (explicit ReBAC shares)
        # are re-added below via _get_cross_zone_shared_paths.
        if not _zone_view.unrestricted:
            all_files = [m for m in all_files if _zone_view.allows(m.get("zone_id"), m.get("path"))]

        # Issue #904: Fetch cross-zone shared files
        if list_zone_id and subject_type and subject_id:
            _ct_start = _time.time()
            cross_zone_paths = self._get_cross_zone_shared_paths(
                subject_type=subject_type,
                subject_id=subject_id,
                zone_id=list_zone_id,
                prefix=list_prefix,
            )
            logger.info(
                f"[LIST-TIMING] cross_zone_lookup: {(_time.time() - _ct_start) * 1000:.1f}ms, "
                f"{len(cross_zone_paths) if cross_zone_paths else 0} paths"
            )
            if cross_zone_paths:
                from nexus.contracts.types import OperationContext as _OC

                _xz_ctx = _OC(user_id="system", groups=[], is_system=True)
                existing_paths = {meta["path"] for meta in all_files}
                for ct_path in cross_zone_paths:
                    if ct_path not in existing_paths:
                        try:
                            ct_stat = self._nexus_fs.sys_stat(ct_path, context=_xz_ctx)
                            if ct_stat:
                                # Cross-zone entries flow through the same
                                # detail-dict shape as sys_readdir(details=True)
                                # so downstream consumers stay dict-typed.
                                all_files.append(
                                    {
                                        "path": ct_path,
                                        "size": ct_stat.get("size", 0),
                                        "content_id": ct_stat.get("content_id"),
                                        "version": ct_stat.get("version", 1),
                                        "entry_type": ct_stat.get("entry_type", 0),
                                        "zone_id": ct_stat.get("zone_id"),
                                        "mime_type": ct_stat.get("mime_type"),
                                        "modified_at": ct_stat.get("modified_at"),
                                        "created_at": ct_stat.get("created_at"),
                                    }
                                )
                        except Exception:
                            logger.debug("Skipping deleted cross-zone path: %s", ct_path)

        # Filter out internal system entries
        from nexus.contracts.constants import SYSTEM_PATH_PREFIX

        all_files = [m for m in all_files if not str(m["path"]).startswith(SYSTEM_PATH_PREFIX)]

        # Apply recursive filter
        if recursive:
            results = all_files
        else:
            results = []
            for meta in all_files:
                _mp = str(meta["path"])
                rel_path = _mp[len(path) :] if path != "/" else _mp[1:]
                if "/" not in rel_path:
                    results.append(meta)
            logger.info(
                f"[LIST-DEBUG] after non-recursive filter: {len(results)} results "
                f"(from {len(all_files)} all_files)"
            )

        # Issue #900: Single Permission Pass
        allowed_set, backend_dirs = self._list_permission_filter(
            all_files,
            results,
            path,
            recursive,
            context,
            _accessible_int_ids,
            _preapproved_dirs,
        )
        if self._enforce_permissions:
            results_before = len(results)
            results = [meta for meta in results if meta["path"] in allowed_set]
            logger.info(
                f"[LIST-DEBUG] after perm filter: {len(results)} results (was {results_before})"
            )
        else:
            if not recursive:
                backend_dirs = self._get_backend_directory_entries(path)

        # Sort by path
        _sort_start = _time.time()
        results.sort(key=lambda m: str(m["path"]))
        logger.info(f"[LIST-TIMING] sort_results: {(_time.time() - _sort_start) * 1000:.1f}ms")

        # Add directories to results
        directories = self._list_infer_directories(
            all_files,
            results,
            path,
            recursive,
            allowed_set,
            backend_dirs,
            context,
            zone_id=list_zone_id,
        )

        logger.info(f"[LIST-DEBUG] FINAL directories: {sorted(directories)[:10]}")

        # Build output
        if details:
            return self._list_build_details(results, directories, path, context, _list_start)
        else:
            return self._list_build_paths(results, directories, path, context, _list_start)

    # =========================================================================
    # List Helpers (extracted from mixin's monolithic list())
    # =========================================================================

    def _extract_zone_info(self, context: Any) -> tuple[str, str | None, str | None]:
        """Extract zone_id, subject_type, subject_id from context for DB filtering.

        zone_id always returns a non-None value (defaults to "root").
        """
        list_zone_id: str = ROOT_ZONE_ID
        subject_type: str | None = None
        subject_id: str | None = None
        if self._enforce_permissions and context:
            if hasattr(context, "zone_id") and context.zone_id:
                list_zone_id = context.zone_id
            if hasattr(context, "subject_type") and hasattr(context, "subject_id"):
                subject_type = context.subject_type
                subject_id = context.subject_id or context.user_id
            elif hasattr(context, "user_id"):
                subject_type = "user"
                subject_id = context.user_id
        return list_zone_id, subject_type, subject_id

    def _list_dir_parallel(
        self,
        backend: Any,
        root_path: str,
        backend_path: str,
        context: Any,
        recursive: bool = True,
    ) -> builtins.list[str]:
        """Parallel directory traversal using ThreadPoolExecutor (Issue #901).

        Uses BFS with batched parallel I/O for recursive directory listing.
        For non-recursive listings, performs a single list_dir call.

        Args:
            backend: Backend instance with list_dir() method
            root_path: Virtual path prefix (e.g., "/zone/agent/connector/gmail")
            backend_path: Starting backend-relative path
            context: OperationContext for authentication
            recursive: If True, recurse into subdirectories in parallel

        Returns:
            List of virtual paths (directories have trailing slash stripped)
        """
        # Single-level listing: no parallelization needed
        entries = backend.list_dir(backend_path, context=context)
        results: builtins.list[str] = []

        if not recursive:
            for entry in entries:
                full_path = f"{root_path.rstrip('/')}/{entry}"
                if entry.endswith("/"):
                    results.append(full_path.rstrip("/"))
                else:
                    results.append(full_path)
            return results

        # Process root level entries, collecting subdirectories for parallel traversal
        pending_dirs: builtins.list[tuple[str, str]] = []
        for entry in entries:
            full_path = f"{root_path.rstrip('/')}/{entry}"
            if entry.endswith("/"):
                results.append(full_path.rstrip("/"))
                subdir_backend_path = (
                    f"{backend_path.rstrip('/')}/{entry.rstrip('/')}"
                    if backend_path
                    else entry.rstrip("/")
                )
                pending_dirs.append((full_path.rstrip("/"), subdir_backend_path))
            else:
                results.append(full_path)

        if not pending_dirs:
            return results

        # BFS with parallel I/O using shared thread pool (Issue #899)
        start_time = time.time()
        depth = 0
        executor = self._get_list_thread_pool()

        while pending_dirs and depth < LIST_PARALLEL_MAX_DEPTH:
            depth += 1
            futures = {
                executor.submit(backend.list_dir, bp, context=context): (vp, bp)
                for vp, bp in pending_dirs
            }
            pending_dirs = []

            for future in as_completed(futures):
                virtual_path, b_path = futures[future]
                try:
                    dir_entries = future.result(timeout=30)
                    for entry in dir_entries:
                        full_path = f"{virtual_path.rstrip('/')}/{entry}"
                        if entry.endswith("/"):
                            results.append(full_path.rstrip("/"))
                            subdir_bp = (
                                f"{b_path.rstrip('/')}/{entry.rstrip('/')}"
                                if b_path
                                else entry.rstrip("/")
                            )
                            pending_dirs.append((full_path.rstrip("/"), subdir_bp))
                        else:
                            results.append(full_path)
                except Exception as e:
                    logger.warning(f"[LIST-PARALLEL] Failed to list '{virtual_path}': {e}")

        if depth >= LIST_PARALLEL_MAX_DEPTH:
            logger.warning(
                f"[LIST-PARALLEL] Hit max depth {LIST_PARALLEL_MAX_DEPTH}, truncating traversal"
            )

        elapsed = time.time() - start_time
        logger.debug(f"[LIST-PARALLEL] Completed: {len(results)} entries in {elapsed:.3f}s")

        return results

    def _list_dynamic_connector(
        self,
        path: str,
        route: Any,
        recursive: bool,
        details: bool,
        context: Any,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """Handle listing for dynamic API-backed connectors (e.g., Gmail, GCS)."""
        # Permission check on mount path
        if self._enforce_permissions and context:
            mount_path = route.mount_point.rstrip("/")
            if not mount_path:
                mount_path = path.rstrip("/")
            if context.is_admin:
                has_permission = True
            elif context.subject_id is None:
                has_permission = False
            else:
                has_permission = self._permission_enforcer.check(
                    mount_path, Permission.TRAVERSE, context
                )
                if not has_permission:
                    has_permission = self._has_descendant_access(
                        mount_path, Permission.READ, context
                    )
            if not has_permission:
                raise PermissionDeniedError(
                    f"Access denied: User '{context.user_id}' does not have "
                    f"TRAVERSE permission for '{path}'"
                )

        # Build list context
        from dataclasses import replace

        if context:
            list_context = replace(context, backend_path=route.backend_path)
        else:
            from nexus.contracts.types import OperationContext

            list_context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path
            )

        # Issue #3266: Metastore-first listing.
        # Prefer metastore entries when available (populated by sync
        # infrastructure).  Fall back to live API on cache miss.
        all_paths = self._list_from_metastore_or_api(
            path=path,
            route=route,
            list_context=list_context,
            recursive=recursive,
        )

        # Permission filtering
        if self._enforce_permissions and context:
            from nexus.contracts.types import OperationContext

            filter_ctx = context if isinstance(context, OperationContext) else self._default_context
            assert filter_ctx is not None  # guaranteed by isinstance or _default_context
            dir_paths = [p for p in all_paths if p.endswith("/")]
            file_paths = [p for p in all_paths if not p.endswith("/")]
            filtered_files = self._permission_enforcer.filter_list(file_paths, filter_ctx)
            filtered_dirs = [
                d
                for d in dir_paths
                if self._permission_enforcer.has_accessible_descendants(d.rstrip("/"), filter_ctx)
            ]
            all_paths = filtered_dirs + filtered_files

        if details:
            return self._list_connector_details(all_paths)
        return all_paths

    def _list_from_metastore_or_api(
        self,
        path: str,
        route: Any,
        list_context: Any,
        recursive: bool,
    ) -> builtins.list[str]:
        """List directory entries via live API."""
        return self._list_dir_parallel(
            backend=route.backend,
            root_path=path,
            backend_path=route.backend_path,
            context=list_context,
            recursive=recursive,
        )

    def resolve_physical_path(self, virtual_path: str) -> str | None:
        """Resolve display path → raw backend path via file_paths table.

        Used by API handlers to translate human-readable connector paths
        back to the raw backend path for read_content(). Keeps the
        resolution in the service layer, not the kernel.
        """
        try:
            from nexus.lib.env import get_database_url

            db_url = get_database_url()
            if not db_url:
                return None

            from sqlalchemy import text

            if not hasattr(self, "_fp_engine") or self._fp_engine is None:
                from sqlalchemy import create_engine

                self._fp_engine = create_engine(
                    db_url, pool_size=2, max_overflow=3, pool_pre_ping=True
                )

            with self._fp_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT physical_path FROM file_paths WHERE virtual_path = :vp LIMIT 1"),
                    {"vp": virtual_path},
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
            return None
        except Exception:
            return None

    def _list_connector_details(
        self,
        all_paths: builtins.list[str],
    ) -> builtins.list[dict[str, Any]]:
        """Build detailed results for dynamic connector paths."""
        from nexus.contracts.types import OperationContext as _OC

        _stat_ctx = _OC(user_id="system", groups=[], is_system=True)
        results_with_details = []
        for entry_path in all_paths:
            file_stat = self._nexus_fs.sys_stat(entry_path, context=_stat_ctx)
            is_dir = bool(file_stat and file_stat.get("is_directory", False))
            name = entry_path.rstrip("/").split("/")[-1]
            results_with_details.append(
                {
                    "path": entry_path,
                    "size": file_stat.get("size", 0) if file_stat else 0,
                    "modified_at": file_stat.get("modified_at") if file_stat else None,
                    "created_at": file_stat.get("created_at") if file_stat else None,
                    "content_id": file_stat.get("content_id") if file_stat else None,
                    "mime_type": file_stat.get("mime_type") if file_stat else None,
                    "is_directory": is_dir,
                    "name": name,
                    "type": "directory" if is_dir else "file",
                    "updated_at": file_stat.get("modified_at") if file_stat else None,
                }
            )
        return results_with_details

    def _list_fast_path(
        self,
        path: str,
        list_zone_id: str,
        context: Any,
        _rebac_manager: Any,
    ) -> tuple[builtins.list[Any], set[str], bool, int | None]:
        """Non-recursive list using sparse directory index + Tiger bitmap."""
        _preapproved_dirs: set[str] = set()
        _revision_before: int | None = None

        if _rebac_manager and hasattr(_rebac_manager, "_get_zone_revision_for_grant"):
            _revision_before = _rebac_manager._get_zone_revision_for_grant(list_zone_id)

        import time as _time

        _idx_start = _time.time()
        # ``list_directory_entries`` was a Raft-side sparse-index helper
        # that the kernel metastore doesn't expose. Returning ``None``
        # keeps the documented "miss → fall back" branch live; ``path``
        # and ``list_zone_id`` become unused below the early return.
        dir_entries = None
        _idx_elapsed = (_time.time() - _idx_start) * 1000

        if dir_entries is None:
            logger.info(
                f"[LIST-TIMING] list_directory_entries(): {_idx_elapsed:.1f}ms (sparse index MISS)"
            )
            return [], set(), False, _revision_before

        logger.info(
            f"[LIST-TIMING] list_directory_entries(): {_idx_elapsed:.1f}ms, "
            f"{len(dir_entries)} entries (sparse index HIT)"
        )

        # Issue #3706: Batch permission check — collect all directory prefixes
        # and check them in a single call instead of N serial calls.
        _perm_start = _time.time()
        _resolved_entries: builtins.list[tuple[str, dict[str, Any]]] = []
        _dir_prefixes: builtins.list[str] = []
        for entry in dir_entries:
            entry_path = f"{path.rstrip('/')}/{entry['name']}"
            _resolved_entries.append((entry_path, entry))
            if entry["type"] == "directory":
                _dir_prefixes.append(entry_path)

        _accessible_dirs: dict[str, bool] = {}
        if _dir_prefixes and self._permission_enforcer:
            _accessible_dirs = self._permission_enforcer.has_accessible_descendants_batch(
                _dir_prefixes, context
            )

        all_files = []
        for entry_path, entry in _resolved_entries:
            if entry["type"] == "directory":
                if _accessible_dirs.get(entry_path, True):
                    _preapproved_dirs.add(entry_path)
                    all_files.append(
                        {
                            "path": entry_path,
                            "size": 0,
                            "created_at": entry.get("created_at"),
                            "content_id": None,
                            "mime_type": "inode/directory",
                            "entry_type": 1,
                        }
                    )
            else:
                all_files.append(
                    {
                        "path": entry_path,
                        "size": 0,
                        "created_at": entry.get("created_at"),
                        "content_id": None,
                        "mime_type": None,
                        "entry_type": 0,
                    }
                )
        logger.info(
            f"[LIST-TIMING] has_accessible_descendants_batch(): "
            f"{(_time.time() - _perm_start) * 1000:.1f}ms for {len(dir_entries)} entries "
            f"({len(_dir_prefixes)} dirs checked in 1 batch call)"
        )

        # Check revision consistency
        _use_fast_path = True
        if (
            _revision_before is not None
            and _rebac_manager
            and hasattr(_rebac_manager, "_get_zone_revision_for_grant")
        ):
            _revision_after = _rebac_manager._get_zone_revision_for_grant(list_zone_id)
            if _revision_after != _revision_before:
                logger.warning(
                    f"[LIST-TIMING] Revision changed ({_revision_before} -> {_revision_after}), "
                    f"falling back to full list"
                )
                _use_fast_path = False

        return all_files, _preapproved_dirs, _use_fast_path, _revision_before

    def _sys_readdir_entries(self, list_prefix: str) -> builtins.list[dict[str, Any]]:
        """Recursive sys_readdir → list of detail dicts for the slow-path scan.

        The detail dict (sys_readdir details=True) is the §2.5 syscall's
        native output shape and the single shape the list pipeline works
        in — no conversion to FileMetadata. Runs under is_system=True so
        the wrapper's zone filter is skipped — the service applies its own
        stronger filter (zone post-filter + tiger_cache predicate-pushdown)
        downstream.
        """
        from nexus.contracts.types import OperationContext

        if self._nexus_fs is None:
            return []
        ctx = OperationContext(user_id="system", groups=[], is_system=True)
        root = list_prefix or "/"
        try:
            recursive_entries = self._nexus_fs.sys_readdir(
                root,
                recursive=True,
                details=True,
                context=ctx,
            )
        except Exception:
            recursive_entries = []
        recursive_dicts = [entry for entry in recursive_entries if isinstance(entry, dict)]
        if recursive_dicts:
            return recursive_dicts

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        pending: list[tuple[str, int]] = [(root, 0)]

        def _stat_entry(path: str) -> dict[str, Any] | None:
            try:
                stat = self._nexus_fs.sys_stat(path, context=ctx)
            except Exception:
                return None
            if not isinstance(stat, dict) or not stat:
                return None
            return {
                "path": stat.get("path", path),
                "size": stat.get("size", 0),
                "content_id": stat.get("content_id"),
                "entry_type": stat.get("entry_type", 1 if stat.get("is_directory") else 0),
                "zone_id": stat.get("zone_id"),
                "owner_id": stat.get("owner_id"),
                "mime_type": stat.get("mime_type"),
                "created_at": stat.get("created_at"),
                "modified_at": stat.get("modified_at"),
                "version": stat.get("version", 1),
                "gen": stat.get("gen", 0),
            }

        while pending:
            current, depth = pending.pop(0)
            if depth >= LIST_PARALLEL_MAX_DEPTH:
                logger.warning(
                    "[LIST-PARALLEL] Hit max depth %s at %s, truncating traversal",
                    LIST_PARALLEL_MAX_DEPTH,
                    current,
                )
                continue
            try:
                entries = self._nexus_fs.sys_readdir(
                    current,
                    recursive=False,
                    details=True,
                    context=ctx,
                )
            except Exception:
                entries = []

            # The real sandbox kernel currently returns no detail rows for
            # root but can return path-only rows; recover details through
            # sys_stat while staying on the syscall surface.
            if not entries and current == "/":
                try:
                    paths = self._nexus_fs.sys_readdir(
                        current,
                        recursive=False,
                        details=False,
                        context=ctx,
                    )
                except Exception:
                    paths = []
                entries = [
                    stat
                    for path in paths
                    if isinstance(path, str)
                    for stat in [_stat_entry(path)]
                    if stat is not None
                ]

            for entry in entries:
                if isinstance(entry, dict):
                    path = str(entry.get("path") or "")
                    if path and path not in seen:
                        seen.add(path)
                        out.append(entry)
                        if _entry_is_dir(entry):
                            pending.append((path, depth + 1))
        return out

    def _list_slow_path(
        self,
        list_prefix: str,
        list_zone_id: str,
        subject_type: str | None,
        subject_id: str | None,
        _revision_before: int | None,
        _rebac_manager: Any,
        use_tiger_pushdown: bool = True,
    ) -> tuple[builtins.list[Any], set[int] | None]:
        """Full recursive metadata scan with predicate pushdown optimization.

        ``use_tiger_pushdown=False`` (Issue #4739, strong consistency) skips
        the Tiger bitmap pre-filter so every candidate reaches ``filter_list``.
        """
        import os as _os
        import time as _time

        _accessible_int_ids: set[int] | None = None
        _pushdown_disabled = _os.getenv("NEXUS_DISABLE_PREDICATE_PUSHDOWN", "").lower() in (
            "1",
            "true",
        )

        if (
            self._enforce_permissions
            and subject_type
            and subject_id
            and not _pushdown_disabled
            and use_tiger_pushdown
        ):
            _pushdown_start = _time.time()
            tiger_cache = getattr(_rebac_manager, "_tiger_cache", None) if _rebac_manager else None
            if tiger_cache is not None:
                try:
                    if (
                        _revision_before is None
                        and _rebac_manager
                        and hasattr(_rebac_manager, "_get_zone_revision_for_grant")
                    ):
                        _revision_before = _rebac_manager._get_zone_revision_for_grant(list_zone_id)
                    _accessible_int_ids = tiger_cache.get_accessible_int_ids(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission="read",
                        resource_type="file",
                        zone_id=list_zone_id,
                    )
                    if _accessible_int_ids is not None:
                        if len(_accessible_int_ids) > 0:
                            logger.info(
                                f"[PREDICATE-PUSHDOWN] Got {len(_accessible_int_ids)} accessible "
                                f"int IDs in {(_time.time() - _pushdown_start) * 1000:.1f}ms"
                            )
                        else:
                            logger.info("[PREDICATE-PUSHDOWN] Empty int IDs, falling back")
                            _accessible_int_ids = None
                except Exception as e:
                    logger.warning(f"[PREDICATE-PUSHDOWN] Failed to get int IDs: {e}")
                    _accessible_int_ids = None

        _meta_start = _time.time()
        # §2.5 mediation: reach MetaStore through the syscall surface, not
        # directly via kernel.metastore_*. is_system=True bypasses the
        # wrapper's zone filter — the service applies its own stronger
        # filter below (zone post-filter + tiger_cache predicate-pushdown),
        # so the wrapper filter would only add cost.
        all_files = self._sys_readdir_entries(list_prefix)
        logger.info(
            f"[LIST-TIMING] metadata.list(): {(_time.time() - _meta_start) * 1000:.1f}ms, "
            f"{len(all_files)} files"
        )

        # Fix nexi-lab/nexus#3733 Bug B: drop synthetic metadata-store entries
        # whose paths are not valid virtual filesystem paths. The ReBAC brick
        # stores namespace configurations as ``FileMetadata(path="ns:rebac:{type}",
        # backend_name="_namespace")`` via ``MetastoreNamespaceStore``. When a
        # non-admin user's list request walks from ``/`` (e.g. the new POST
        # ``/api/v2/search/grep`` endpoint defaults to ``path="/"``), those
        # synthetic entries leak into the candidate set, then the permission
        # filter's ``router.validate_path`` call rejects them with
        # ``InvalidPathError: Path must be absolute: ns:rebac:memory``.
        #
        # The correct fix is to scope them: any entry whose path does
        # not start with ``/`` is a synthetic/pseudo-path that should never
        # enter the filesystem filter pipeline.
        _pre_synthetic = len(all_files)
        all_files = [f for f in all_files if str(f.get("path", "")).startswith("/")]
        if len(all_files) != _pre_synthetic:
            logger.debug(
                f"[LIST-SYNTHETIC] dropped {_pre_synthetic - len(all_files)} "
                f"synthetic metadata entries (e.g. ns:rebac:*)"
            )

        # Predicate pushdown: filter by accessible_int_ids at service layer
        if _accessible_int_ids is not None:
            tiger_cache = getattr(_rebac_manager, "_tiger_cache", None) if _rebac_manager else None
            if tiger_cache is not None:
                before_count = len(all_files)
                all_files = [
                    f
                    for f in all_files
                    if tiger_cache._resource_map.get_or_create_int_id("file", f["path"])
                    in _accessible_int_ids
                ]
                logger.info(
                    f"[PREDICATE-PUSHDOWN] Service-layer filter: "
                    f"{before_count} -> {len(all_files)} files "
                    f"({len(_accessible_int_ids)} accessible int IDs)"
                )

            # Issue #1147: Check if revision changed during query (TOCTOU race detection)
            if (
                _revision_before is not None
                and _rebac_manager
                and hasattr(_rebac_manager, "_get_zone_revision_for_grant")
            ):
                _revision_after = _rebac_manager._get_zone_revision_for_grant(list_zone_id)
                if _revision_after != _revision_before:
                    logger.warning(
                        "[PREDICATE-PUSHDOWN] Revision changed, re-running without filter"
                    )
                    _meta_start = _time.time()
                    all_files = self._sys_readdir_entries(list_prefix)
                    # Fix nexi-lab/nexus#3733 Bug B: same synthetic-entry
                    # guard as the primary list path above.
                    all_files = [f for f in all_files if str(f.get("path", "")).startswith("/")]
                    logger.info(
                        f"[LIST-TIMING] metadata.list() retry: "
                        f"{(_time.time() - _meta_start) * 1000:.1f}ms, {len(all_files)} files"
                    )
                    _accessible_int_ids = None

        return all_files, _accessible_int_ids

    def _list_permission_filter(
        self,
        all_files: builtins.list[Any],
        results: builtins.list[Any],  # noqa: ARG002 - Reserved for future predicate pushdown
        path: str,
        recursive: bool,
        context: Any,
        _accessible_int_ids: set[int] | None,
        _preapproved_dirs: set[str],
    ) -> tuple[set[str], set[str]]:
        """Single permission pass for all candidate paths (Issue #900)."""
        allowed_set: set[str] = set()
        backend_dirs: set[str] = set()

        if not self._enforce_permissions:
            return allowed_set, backend_dirs

        import time

        from nexus.contracts.types import OperationContext

        perm_start = time.time()
        ctx_raw = context or self._default_context
        assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
        ctx: OperationContext = ctx_raw

        candidate_paths: set[str] = set()
        candidate_paths.update(meta["path"] for meta in all_files)

        if not recursive:
            backend_dirs = self._get_backend_directory_entries(path)
            candidate_paths.update(backend_dirs)

        # Single permission filter call
        filter_start = time.time()
        if _accessible_int_ids is not None:
            # ``all_files`` reached us already narrowed to the caller's
            # ZoneView in ``list()`` (#4740), which subsumes the former
            # #3786 federation-token allow-list intersection: predicate-
            # pushdown paths outside the token's readable zones were
            # dropped upstream, root-tagged rows included.
            allowed_set = {meta["path"] for meta in all_files}
            logger.info(
                f"[PREDICATE-PUSHDOWN] Skipped filter_list() - "
                f"using {len(allowed_set)} pre-filtered paths"
            )
        else:
            allowed_list = self._permission_enforcer.filter_list(list(candidate_paths), ctx)
            allowed_set = set(allowed_list)
        filter_elapsed = time.time() - filter_start

        if _preapproved_dirs:
            allowed_set.update(_preapproved_dirs)

        logger.debug(
            f"[PERF-LIST] Permission filter: {filter_elapsed:.3f}s, "
            f"allowed {len(allowed_set)}/{len(candidate_paths)} paths"
        )
        logger.debug(f"[PERF-LIST] Total: {time.time() - perm_start:.3f}s")

        return allowed_set, backend_dirs

    def _list_infer_directories(
        self,
        all_files: builtins.list[Any],
        results: builtins.list[Any],
        path: str,
        recursive: bool,
        allowed_set: set[str],
        backend_dirs: set[str],
        context: Any,
        zone_id: str = ROOT_ZONE_ID,
    ) -> set[str]:
        """Infer directory entries from file paths and backend."""
        import time as _time

        _dir_start = _time.time()
        directories: set[str] = set()

        for meta in results:
            if _entry_is_dir(meta):
                directories.add(meta["path"])

        if not recursive:
            if self._enforce_permissions and context:
                for meta in all_files:
                    _mp = str(meta["path"])
                    if _mp in allowed_set:
                        rel_path = _mp[len(path) :] if path != "/" else _mp[1:]
                        if "/" in rel_path:
                            dir_name = rel_path.split("/")[0]
                            dir_path = path + dir_name if path != "/" else "/" + dir_name
                            directories.add(dir_path)

                self._list_check_backend_dirs(
                    backend_dirs,
                    allowed_set,
                    directories,
                    context,
                    zone_id=zone_id,
                )
            else:
                for meta in all_files:
                    _mp = str(meta["path"])
                    rel_path = _mp[len(path) :] if path != "/" else _mp[1:]
                    if "/" in rel_path:
                        dir_name = rel_path.split("/")[0]
                        dir_path = path + dir_name if path != "/" else "/" + dir_name
                        directories.add(dir_path)
                directories.update(backend_dirs)

        logger.info(
            f"[LIST-TIMING] dir_processing: {(_time.time() - _dir_start) * 1000:.1f}ms, "
            f"{len(directories)} dirs"
        )
        return directories

    def _list_check_backend_dirs(
        self,
        backend_dirs: set[str],
        allowed_set: set[str],
        directories: set[str],
        context: Any,
        zone_id: str = ROOT_ZONE_ID,
    ) -> None:
        """Check backend directories for access using bulk TRAVERSE check."""
        import time as _time

        # Precompute ancestor directories of allowed paths
        allowed_ancestors: set[str] = set()
        for p in allowed_set:
            parts = p.split("/")
            for i in range(2, len(parts)):
                ancestor = "/".join(parts[:i])
                if ancestor:
                    allowed_ancestors.add(ancestor)

        _bd_start = _time.time()
        _traverse_checks = 0
        _prefix_checks = 0
        dirs_needing_traverse: list[str] = []

        for dir_path in backend_dirs:
            if dir_path in allowed_set:
                directories.add(dir_path)
                continue
            if dir_path in allowed_ancestors:
                _prefix_checks += 1
                directories.add(dir_path)
                continue
            dirs_needing_traverse.append(dir_path)

        # Two-phase TRAVERSE optimization (Fix #1147)
        user_zone = zone_id
        _skipped_cross_zone = 0
        _ZONE_PREFIXES = ZONE_AWARE_PREFIXES
        dirs_to_check: list[str] = []

        for dir_path in dirs_needing_traverse:
            if user_zone:
                skip = False
                for tp in _ZONE_PREFIXES:
                    if dir_path.startswith(tp):
                        rest = dir_path[len(tp) :]
                        path_zone = rest.split("/")[0] if rest else None
                        if path_zone and path_zone != user_zone:
                            _skipped_cross_zone += 1
                            skip = True
                        break
                if skip:
                    continue
            dirs_to_check.append(dir_path)

        # Bulk TRAVERSE check via rebac_check_bulk
        _traverse_checks = len(dirs_to_check)
        _rebac_manager = (
            getattr(self._permission_enforcer, "rebac_manager", None)
            if self._permission_enforcer
            else None
        )
        if dirs_to_check and _rebac_manager and hasattr(_rebac_manager, "rebac_check_bulk"):
            subject = context.get_subject()
            bulk_checks = []
            for dp in dirs_to_check:
                for perm in ("traverse", "read", "write"):
                    bulk_checks.append((subject, perm, ("file", dp)))
            bulk_results = _rebac_manager.rebac_check_bulk(bulk_checks, zone_id)
            for dp in dirs_to_check:
                if (
                    bulk_results.get((subject, "traverse", ("file", dp)), False)
                    or bulk_results.get((subject, "read", ("file", dp)), False)
                    or bulk_results.get((subject, "write", ("file", dp)), False)
                ):
                    directories.add(dp)
        else:
            for dir_path in dirs_to_check:
                if self._permission_enforcer.check(dir_path, Permission.TRAVERSE, context):
                    directories.add(dir_path)

        logger.info(
            f"[LIST-TIMING] backend_dir_checks: {(_time.time() - _bd_start) * 1000:.1f}ms, "
            f"traverse={_traverse_checks}, prefix={_prefix_checks}, "
            f"skipped_cross_zone={_skipped_cross_zone}"
        )

    def _list_build_details(
        self,
        results: builtins.list[Any],
        directories: set[str],
        path: str,
        context: Any,
        _list_start: float,
    ) -> builtins.list[dict[str, Any]]:
        """Build detailed results with metadata."""
        import time as _time

        _details_start = _time.time()

        file_results = [
            {
                "path": meta["path"],
                "size": meta.get("size", 0),
                "modified_at": meta.get("modified_at"),
                "created_at": meta.get("created_at"),
                "content_id": meta.get("content_id"),
                "mime_type": meta.get("mime_type"),
                "is_directory": False,
            }
            for meta in results
            if not _entry_is_dir(meta)
        ]
        dir_results = [
            {
                "path": dir_path,
                "size": 0,
                "modified_at": None,
                "created_at": None,
                "content_id": None,
                "mime_type": None,
                "is_directory": True,
            }
            for dir_path in sorted(directories)
        ]
        all_results = file_results + dir_results
        all_results.sort(key=lambda x: str(x["path"]))
        logger.info(
            f"[LIST-TIMING] TOTAL: {(_time.time() - _list_start) * 1000:.1f}ms for path={path}"
        )
        self._record_read_if_tracking(context, "directory", path, "list")
        return all_results

    def _list_build_paths(
        self,
        results: builtins.list[Any],
        directories: set[str],
        path: str,
        context: Any,
        _list_start: float,
    ) -> builtins.list[str]:
        """Build path-only results."""
        import time as _time

        file_paths = [meta["path"] for meta in results if not _entry_is_dir(meta)]
        all_paths = file_paths + sorted(directories)
        all_paths.sort()
        logger.info(
            f"[LIST-TIMING] TOTAL: {(_time.time() - _list_start) * 1000:.1f}ms for path={path}"
        )
        self._record_read_if_tracking(context, "directory", path, "list")
        return all_paths

    def _list_paginated(
        self,
        path: str,
        recursive: bool,
        details: bool,
        limit: int,
        cursor: str | None,
        context: Any,
        zone_view: Any = None,
    ) -> Any:
        """Paginated list with over-fetch strategy for permission filtering (Issue #937).

        ``zone_view`` is the caller's resolved ZoneView (#4740); when omitted
        it is resolved from *context* here so direct callers stay fail-closed.
        """
        from nexus.contracts.constants import SYSTEM_PATH_PREFIX
        from nexus.contracts.metadata import DT_DIR
        from nexus.contracts.types import OperationContext
        from nexus.core.pagination import PaginatedResult
        from nexus.lib.pagination import encode_cursor

        if zone_view is None:
            zone_view = resolve_zone_view(
                context, operation="search.list", init_cred=self._default_context
            )
        context = context or self._default_context
        import time as _time

        _start = _time.time()  # noqa: F841 — retained for parity with non-paginated list timing

        list_zone_id, _, _ = self._extract_zone_info(context)

        if path and path != "/":
            path = self._validate_path(path)
        if path and not path.endswith("/"):
            path = path + "/"
        list_prefix = path if path != "/" else ""

        buffer_multiplier = 1.5
        fetch_limit = int(limit * buffer_multiplier)
        collected_items: builtins.list[dict[str, Any]] = []
        has_more = True

        # Decode encoded cursor to plain path for sys_readdir's keyset cursor.
        current_cursor_path: str | None = None
        if cursor:
            from nexus.lib.pagination import CursorError, decode_cursor

            try:
                filters = {"prefix": list_prefix, "recursive": recursive, "zone_id": list_zone_id}
                current_cursor_path = decode_cursor(cursor, filters).path
            except CursorError:
                current_cursor_path = None

        # §2.5: paginated scan goes through sys_readdir (native limit/cursor
        # support), not the metastore_list_iter → metastore_list_paginated
        # HAL bypass. is_system=True so the wrapper skips its zone filter —
        # the service applies its own filter_list permission pass below.
        sys_ctx = OperationContext(user_id="system", groups=[], is_system=True)
        while len(collected_items) < limit and has_more:
            batch = self._nexus_fs.sys_readdir(
                list_prefix or "/",
                recursive=recursive,
                details=True,
                limit=fetch_limit,
                cursor=current_cursor_path,
                context=sys_ctx,
            )

            # sys_readdir already drops cfg:/ns: internal paths; additionally
            # filter SYSTEM_PATH_PREFIX and reject any non-/ entries
            # (nexi-lab/nexus#3733 Bug B).
            batch_items = [
                item
                for item in batch.items
                # Fix nexi-lab/nexus#3733 Bug B: drop synthetic metadata entries
                # (e.g. ns:rebac:*) whose paths are not valid virtual paths.
                if str(item.get("path", "")).startswith("/")
                and not str(item.get("path", "")).startswith(SYSTEM_PATH_PREFIX)
                # Issue #4740: the scan runs under is_system (unrestricted), so
                # the caller's zone predicate has to be applied to each page.
                and zone_view.allows(item.get("zone_id"), item.get("path"))
            ]

            if self._enforce_permissions and context:
                paths = [item["path"] for item in batch_items]
                allowed_paths = set(self._permission_enforcer.filter_list(paths, context))
                filtered_items = [item for item in batch_items if item["path"] in allowed_paths]
            else:
                filtered_items = batch_items

            collected_items.extend(filtered_items)
            has_more = batch.has_more
            current_cursor_path = batch.next_cursor  # already a plain path
            if not batch.items:
                break

        result_items = collected_items[:limit]
        final_has_more = has_more or len(collected_items) > limit

        next_cursor = None
        if final_has_more and result_items:
            last_item = result_items[-1]
            filters = {"prefix": list_prefix, "recursive": recursive, "zone_id": list_zone_id}
            next_cursor = encode_cursor(
                last_path=str(last_item["path"]),
                last_path_id=None,
                filters=filters,
            )

        if details:
            # sys_readdir(details=True) emits created_at/modified_at as ISO
            # strings (JSON-safe over RPC); they flow through unchanged.
            items_output = [
                {
                    "path": meta["path"],
                    "size": meta.get("size", 0),
                    "modified_at": meta.get("modified_at"),
                    "created_at": meta.get("created_at"),
                    "content_id": meta.get("content_id"),
                    "mime_type": meta.get("mime_type"),
                    "is_directory": meta.get("entry_type") == DT_DIR,
                }
                for meta in result_items
            ]
        else:
            items_output = [meta["path"] for meta in result_items]

        return PaginatedResult(
            items=items_output,
            next_cursor=next_cursor,
            has_more=final_has_more,
            total_count=None,
        )

    def _get_cross_zone_shared_paths(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
        prefix: str = "",
    ) -> builtins.list[str]:
        """Fetch file paths shared with a user from other zones (Issue #904)."""
        if not self._rebac_manager:
            return []

        cache_key = (subject_type, subject_id, zone_id, prefix)
        cached = self._cross_zone_cache.get(cache_key)
        if cached is not None:
            return cached

        get_paths = getattr(self._rebac_manager, "get_cross_zone_shared_paths", None)
        if not callable(get_paths):
            self._cross_zone_cache[cache_key] = []
            return []

        try:
            paths = get_paths(
                subject_type=subject_type,
                subject_id=subject_id,
                zone_id=zone_id,
                prefix=prefix,
            )
            if paths:
                logger.debug(
                    f"[CROSS-ZONE] Found {len(paths)} shared paths for {subject_type}:{subject_id}"
                )
            self._cross_zone_cache[cache_key] = paths
            return paths
        except Exception as e:
            logger.error(
                "Cross-zone sharing error for %s/%s: %s",
                subject_type,
                subject_id,
                e,
                exc_info=True,
            )
            return []

    # =========================================================================
    # Public API: Glob Pattern Matching
    # =========================================================================

    @rpc_expose(description="Find files matching a glob pattern")
    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,  # noqa: ARG002
        files: builtins.list[str] | None = None,
    ) -> builtins.list[str]:
        """Discover files through the owning Kernel's SearchService."""
        response = self._kernel.call_rpc("glob", {"pattern": pattern, "path": path, "files": files})
        return cast(builtins.list[str], response["matches"])

    @rpc_expose(description="Search file contents")
    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        files: builtins.list[str] | None = None,
        block_type: str | None = None,
        section: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Public grep entry point with activity-event instrumentation (#3791).

        Wraps :meth:`_grep_impl` to record SEARCH events on success and
        BLOCKED events on exceptions.  Wall-clock latency is captured.
        ``zone_id`` is derived from the operation context when available;
        ``token_hash`` is not currently extractable from the search
        ``context`` object so it is left as ``None``.
        """
        _start = time.monotonic()
        _zone: str | None = None
        try:
            _zone, _, _ = self._get_routing_params(context)
        except Exception:  # pragma: no cover - never fail emit setup
            _zone = None
        try:
            result = await self._grep_impl(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=max_results,
                search_mode=search_mode,
                context=context,
                before_context=before_context,
                after_context=after_context,
                invert_match=invert_match,
                files=files,
                block_type=block_type,
                section=section,
            )
        except Exception:
            emit(
                kind=EventKind.SEARCH,
                result=Result.BLOCKED,
                actor_token_hash=None,
                subject_zone=_zone,
                latency_ms=int((time.monotonic() - _start) * 1000),
            )
            raise
        emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash=None,
            subject_zone=_zone,
            subject_extra={"hits": len(result) if hasattr(result, "__len__") else None},
            latency_ms=int((time.monotonic() - _start) * 1000),
        )
        return result

    async def _grep_impl(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: Any = None,  # noqa: ARG002
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        files: builtins.list[str] | None = None,
        block_type: str | None = None,
        section: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Forward discovery; filtering reads the current bytes in the Rust plugin."""
        response = await asyncio.to_thread(
            self._kernel.call_rpc,
            "grep",
            {
                "pattern": pattern,
                "path": path,
                "file_pattern": file_pattern,
                "ignore_case": ignore_case,
                "max_results": max_results,
                "search_mode": search_mode,
                "before_context": before_context,
                "after_context": after_context,
                "invert_match": invert_match,
                "files": files,
                "block_type": block_type,
                "section": section,
            },
        )
        return cast(builtins.list[dict[str, Any]], response["results"])

    # =========================================================================
    # Helper Methods: Permission Checking
    # =========================================================================

    def _check_read_permission(self, path: str, context: Any) -> None:
        """Check if user has read permission for path.

        Args:
            path: File or directory path
            context: Operation context

        Raises:
            PermissionDeniedError: If permission denied
        """
        from nexus.contracts.types import OperationContext

        if not self._enforce_permissions or not self._permission_enforcer:
            return

        # Use default context if not provided (embedded mode)
        ctx = context if context is not None else self._default_context

        # Ensure context is OperationContext
        if not isinstance(ctx, OperationContext):
            # Convert or use default
            ctx = self._default_context

        # If still no valid context, cannot check permissions
        if ctx is None:
            raise PermissionDeniedError(
                f"Permission denied: {path} (no context available for permission check)"
            )

        # Check permission using ReBAC
        # Signature: check(path, permission, context)
        has_permission = self._permission_enforcer.check(path, Permission.READ, ctx)
        if not has_permission:
            raise PermissionDeniedError(f"Permission denied: {path}")

    # =========================================================================
    # Helper Methods: Path Validation
    # =========================================================================

    def _validate_path(self, path: str) -> str:
        """Validate and normalize path.

        Delegates to shared path validation utility for security checks.

        Args:
            path: Path to validate

        Returns:
            Normalized path

        Raises:
            InvalidPathError: If path is invalid
        """
        from nexus.core.path_utils import validate_path

        return validate_path(path, allow_root=True)

    # =========================================================================
    # Indexed search
    # =========================================================================

    @rpc_expose(description="Search documents using natural language queries")
    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """Public semantic-search entry point with activity-event instrumentation (#3791).

        Wraps :meth:`_semantic_search_impl` to record SEARCH events on
        success and BLOCKED events on exceptions. Mirrors the ``grep``
        wrapper pattern so all primary SearchService surfaces feed the
        same activity stream.
        """
        _start = time.monotonic()
        _zone: str | None = None
        try:
            _zone, _, _ = self._get_routing_params(context)
        except Exception:  # pragma: no cover - never fail emit setup
            _zone = None
        try:
            result = await self._semantic_search_impl(
                query=query,
                path=path,
                limit=limit,
                filters=filters,
                search_mode=search_mode,
                context=context,
            )
        except Exception:
            emit(
                kind=EventKind.SEARCH,
                result=Result.BLOCKED,
                actor_token_hash=None,
                subject_zone=_zone,
                subject_extra={"mode": search_mode},
                latency_ms=int((time.monotonic() - _start) * 1000),
            )
            raise
        emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash=None,
            subject_zone=_zone,
            subject_extra={
                "mode": search_mode,
                "hits": len(result) if hasattr(result, "__len__") else None,
            },
            latency_ms=int((time.monotonic() - _start) * 1000),
        )
        return result

    async def _semantic_search_impl(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """Query the owning kernel; credentials and permissions are resolved by its host."""
        response = await asyncio.to_thread(
            self._kernel.call_rpc,
            "semantic_search",
            {
                "query": query,
                "path": path,
                "limit": limit,
                "filters": filters,
                "search_mode": search_mode,
                "zone_id": getattr(context, "zone_id", None),
            },
        )
        return cast(builtins.list[dict[str, Any]], response["results"])

    @rpc_expose(description="Index files through the search plugin")
    async def semantic_search_index(
        self,
        path: str = "/",
        recursive: bool = True,
        max_docs: int = 10_000,
        context: "OperationContext | None" = None,
    ) -> dict[str, int]:
        """Index current VFS bytes and return the host's indexed and skipped counts."""
        return cast(
            dict[str, int],
            await asyncio.to_thread(
                self._kernel.call_rpc,
                "semantic_search_index",
                {
                    "path": path,
                    "recursive": recursive,
                    "max_docs": max_docs,
                    "zone_id": getattr(context, "zone_id", None),
                },
            ),
        )

    @rpc_expose(description="Get semantic search indexing statistics")
    async def semantic_search_stats(self) -> dict[str, Any]:
        """Read index statistics through the owning kernel's SearchService."""
        return cast(
            dict[str, Any], await asyncio.to_thread(self._kernel.call_rpc, "semantic_search_stats")
        )
