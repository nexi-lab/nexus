"""Search Service - Extracted from NexusFSSearchMixin (Issue #1287).

This service handles all search operations:
- File listing with pagination and permission filtering
- Glob pattern matching with adaptive algorithms
- Content searching (grep) with 5 strategies
- Semantic search with embeddings

Extracted from: nexus_fs_search.py (2,817 lines)
"""

from __future__ import annotations

import asyncio
import builtins
import fnmatch
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, cast

from nexus.core import glob_fast, grep_fast
from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose
from nexus.search.strategies import (
    GLOB_RUST_THRESHOLD,
    GREP_CACHED_TEXT_RATIO,
    GREP_PARALLEL_THRESHOLD,
    GREP_PARALLEL_WORKERS,
    GREP_SEQUENTIAL_THRESHOLD,
    GREP_ZOEKT_THRESHOLD,
    GlobStrategy,
    SearchStrategy,
)
from nexus.services.gateway import NexusFSGateway
from nexus.services.search_semantic import SemanticSearchMixin

# List directory traversal thresholds (Issue #901)
LIST_PARALLEL_WORKERS = 10  # Thread pool size for parallel directory listing (I/O-bound)
LIST_PARALLEL_MAX_DEPTH = 100  # Safety limit to prevent infinite traversal (e.g., symlink loops)

# Zone-aware path prefixes for cross-zone filtering (Issue #899)
ZONE_AWARE_PREFIXES: tuple[str, ...] = ("/zones/", "/shared/", "/archives/")

# =============================================================================
# Issue #538: Gitignore-style default exclusion patterns
# =============================================================================
DEFAULT_IGNORE_PATTERNS: frozenset[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        ".idea",
        ".vscode",
        "*.swp",
        "*.swo",
        ".DS_Store",
        "Thumbs.db",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.pyc",
        "*.pyo",
        "coverage",
        ".coverage",
        "htmlcov",
        "*.log",
        "logs",
    }
)


def _should_ignore_path(
    path: str, ignore_patterns: frozenset[str] = DEFAULT_IGNORE_PATTERNS
) -> bool:
    """Check if a path should be ignored based on gitignore-style patterns (Issue #538)."""
    segments = path.strip("/").split("/")
    for segment in segments:
        if segment in ignore_patterns:
            return True
        for pattern in ignore_patterns:
            if pattern.startswith("*.") and segment.endswith(pattern[1:]):
                return True
    return False


def _filter_ignored_paths(
    paths: list[str], ignore_patterns: frozenset[str] = DEFAULT_IGNORE_PATTERNS
) -> list[str]:
    """Filter out paths matching gitignore-style patterns (Issue #538)."""
    return [p for p in paths if not _should_ignore_path(p, ignore_patterns)]


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadataProtocol
    from nexus.core.permissions import OperationContext, PermissionEnforcer
    from nexus.core.router import PathRouter
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager


class SearchService(SemanticSearchMixin):
    """Independent search service extracted from NexusFS.

    Handles file listing, glob matching, grep, and semantic search.
    Semantic search methods are provided by SemanticSearchMixin.

    Uses adaptive algorithm selection (Issue #929) to choose optimal
    strategies based on data characteristics. No direct filesystem
    dependencies; uses dependency injection for stores and backends.
    """

    def __init__(
        self,
        metadata_store: FileMetadataProtocol,
        permission_enforcer: PermissionEnforcer | None = None,
        router: PathRouter | None = None,
        rebac_manager: EnhancedReBACManager | None = None,
        enforce_permissions: bool = True,
        default_context: OperationContext | None = None,
        record_store: Any | None = None,
        # Gateway for NexusFS operations (Issue #1287, replaces 8 Callable params)
        gateway: NexusFSGateway | None = None,
    ):
        """Initialize search service.

        Args:
            metadata_store: Metadata store for file information
            permission_enforcer: Permission enforcer for access control
            router: Mount router for backend operations
            rebac_manager: ReBAC manager for relationship-based permissions
            enforce_permissions: Whether to enforce permission checks
            default_context: Default operation context (embedded mode)
            record_store: RecordStoreABC for SQL engine (needed for semantic search)
            gateway: NexusFSGateway for file ops, routing, and dependency tracking
        """
        self.metadata = metadata_store
        self._record_store = record_store
        self._permission_enforcer = permission_enforcer
        self.router = router
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._default_context = default_context

        # Gateway for NexusFS operations (Issue #1287)
        self._gw = gateway

        # Semantic search (initialized later, types declared in SemanticSearchMixin)
        self._semantic_search = None
        self._async_search = None

        # Shared thread pool for parallel grep (Issue #929, fix #14)
        self._thread_pool: ThreadPoolExecutor | None = None

        # Shared thread pool for parallel directory listing (Issue #899)
        self._list_thread_pool: ThreadPoolExecutor | None = None

        # Lock for lazy thread pool initialization (prevents TOCTOU race)
        self._pool_lock = threading.Lock()

        # TTL cache for cross-zone sharing queries (Issue #904)
        self._cross_zone_cache: dict[tuple[str, ...], tuple[float, builtins.list[str]]] = {}

        logger.info("[SearchService] Initialized")

    def _get_thread_pool(self) -> ThreadPoolExecutor:
        """Get or create the shared thread pool for parallel grep operations."""
        if self._thread_pool is None:
            with self._pool_lock:
                if self._thread_pool is None:
                    self._thread_pool = ThreadPoolExecutor(
                        max_workers=GREP_PARALLEL_WORKERS,
                        thread_name_prefix="nexus-search",
                    )
        return self._thread_pool

    def _get_list_thread_pool(self) -> ThreadPoolExecutor:
        """Get or create the shared thread pool for parallel directory listing."""
        if self._list_thread_pool is None:
            with self._pool_lock:
                if self._list_thread_pool is None:
                    self._list_thread_pool = ThreadPoolExecutor(
                        max_workers=LIST_PARALLEL_WORKERS,
                        thread_name_prefix="nexus-list",
                    )
        return self._list_thread_pool

    def close(self) -> None:
        """Release resources held by the search service."""
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=False)
            self._thread_pool = None
        if self._list_thread_pool is not None:
            self._list_thread_pool.shutdown(wait=False)
            self._list_thread_pool = None

    @property
    def _gw_session_factory(self) -> Any:
        """Session factory via gateway (for memory paths, indexing)."""
        if self._gw is not None:
            return self._gw.session_factory
        return None

    @property
    def _gw_backend(self) -> Any:
        """Storage backend via gateway (for memory path content)."""
        if self._gw is not None:
            return self._gw.backend
        return None

    # =========================================================================
    # Delegation Helpers (via NexusFSGateway, Issue #1287)
    # =========================================================================

    def _read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content via gateway."""
        if self._gw is None:
            raise NotImplementedError("gateway not provided to SearchService")
        result = self._gw.read_file(path, context=context, return_metadata=return_metadata)
        if isinstance(result, str):
            return result.encode("utf-8")
        return result

    def _read_bulk(
        self,
        paths: builtins.list[str],
        context: Any = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """Bulk read files via gateway."""
        if self._gw is None:
            raise NotImplementedError("gateway not provided to SearchService")
        return self._gw.read_bulk(
            paths,
            context=context,
            return_metadata=return_metadata,
            skip_errors=skip_errors,
        )

    def _get_routing_params(self, context: Any) -> tuple[str | None, str | None, bool]:
        """Extract zone_id, agent_id, is_admin from context."""
        if self._gw:
            return self._gw.get_routing_params(context)
        return None, None, False

    def _has_descendant_access(self, path: str, permission: Permission, context: Any) -> bool:
        """Check if user has access to any descendant of path."""
        if self._gw:
            return self._gw.has_descendant_access(path, permission, context)
        return False

    def _get_backend_directory_entries(self, path: str) -> set[str]:
        """Get directory entries from backend storage."""
        if self._gw:
            return self._gw.get_backend_directory_entries(path)
        return set()

    def _record_read_if_tracking(
        self,
        context: Any,
        resource_type: str,
        resource_id: str,
        access_type: str = "content",
    ) -> None:
        """Record read for dependency tracking (Issue #1166)."""
        if self._gw:
            self._gw.record_read_if_tracking(context, resource_type, resource_id, access_type)

    # =========================================================================
    # Public API: File Listing
    # =========================================================================

    @rpc_expose(description="List files in directory")
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,  # noqa: ARG002
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any:
        """List files in a directory.

        Supports memory virtual paths, cursor-based pagination (Issue #937),
        dynamic API-backed connectors, and ReBAC permission filtering.

        Args:
            path: Directory path to list (default: "/", supports memory paths)
            recursive: If True, list all files recursively (default: True)
            details: If True, return detailed metadata dicts (default: False)
            prefix: (Deprecated) Path prefix filter for backward compat
            show_parsed: If True, include parsed virtual views (default: True)
            context: Operation context for permission filtering
            limit: Max items per page (enables pagination mode)
            cursor: Continuation token from previous page
        """
        # Issue #937: Pagination mode
        if limit is not None:
            return self._list_paginated(
                path=path,
                recursive=recursive,
                details=details,
                limit=limit,
                cursor=cursor,
                context=context,
            )
        # Phase 2 Integration (v0.4.0): Intercept memory paths
        from nexus.core.memory_router import MemoryViewRouter

        if path and MemoryViewRouter.is_memory_path(path):
            return self._list_memory_path(path, details)

        # Check if path routes to a dynamic API-backed connector
        if path and path != "/" and self.router:
            try:
                zone_id, agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    is_admin=is_admin,
                    check_write=False,
                )
                is_dynamic_connector = (
                    route.backend.user_scoped and route.backend.has_token_manager
                ) or route.backend.has_virtual_filesystem

                if is_dynamic_connector:
                    return self._list_dynamic_connector(path, route, recursive, details, context)
            except PermissionDeniedError:
                raise
            except Exception as e:
                import traceback

                logger.debug(
                    f"Dynamic connector list_dir failed for {path}: {e}\n{traceback.format_exc()}"
                )

        # Issue #904: Extract zone_id for PREWHERE-style DB filtering
        list_zone_id, subject_type, subject_id = self._extract_zone_info(context)

        # Handle backward compatibility with old 'prefix' parameter
        import time as _time

        _list_start = _time.time()
        _preapproved_dirs: set[str] = set()
        _accessible_int_ids: set[int] | None = None

        if prefix is not None:
            if prefix:
                prefix = self._validate_path(prefix)
            _meta_start = _time.time()
            all_files = self.metadata.list(prefix, zone_id=list_zone_id)
            logger.info(
                f"[LIST-TIMING] metadata.list(): {(_time.time() - _meta_start) * 1000:.1f}ms, {len(all_files)} files"
            )
            list_prefix = prefix or ""
        else:
            if path and path != "/":
                path = self._validate_path(path)
            if path and not path.endswith("/"):
                path = path + "/"
            list_prefix = path if path != "/" else ""

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
                f"details={details}, has_list_dir_entries={hasattr(self.metadata, 'list_directory_entries')}, "
                f"has_context={context is not None}"
            )
            if (
                not recursive
                and not details
                and hasattr(self.metadata, "list_directory_entries")
                and context
            ):
                all_files, _preapproved_dirs, _use_fast_path, _revision_before = (
                    self._list_fast_path(path, list_zone_id, context, _rebac_manager)
                )

            if not _use_fast_path:
                all_files, _accessible_int_ids = self._list_slow_path(
                    list_prefix,
                    list_zone_id,
                    subject_type,
                    subject_id,
                    _revision_before,
                    _rebac_manager,
                )
                sample_paths = [m.path for m in all_files[:5]]
                logger.info(f"[LIST-DEBUG] FALLBACK all_files sample: {sample_paths}")

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
                existing_paths = {meta.path for meta in all_files}
                for ct_path in cross_zone_paths:
                    if ct_path not in existing_paths:
                        try:
                            ct_meta = self.metadata.get(ct_path)
                            if ct_meta:
                                all_files.append(ct_meta)
                        except Exception:
                            logger.debug("Skipping deleted cross-zone path: %s", ct_path)

        # Filter out internal system entries
        from nexus.core.nexus_fs_core import SYSTEM_PATH_PREFIX

        all_files = [m for m in all_files if not m.path.startswith(SYSTEM_PATH_PREFIX)]

        # Apply recursive filter
        if prefix is not None or recursive:
            results = all_files
        else:
            results = []
            for meta in all_files:
                rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
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
            results = [meta for meta in results if meta.path in allowed_set]
            logger.info(
                f"[LIST-DEBUG] after perm filter: {len(results)} results (was {results_before})"
            )
        else:
            if not recursive:
                backend_dirs = self._get_backend_directory_entries(path)

        # Sort by path
        _sort_start = _time.time()
        results.sort(key=lambda m: m.path)
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

        zone_id always returns a non-None value (defaults to "default").
        """
        list_zone_id: str = "default"
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
                    f"Access denied: User '{context.user}' does not have "
                    f"TRAVERSE permission for '{path}'"
                )

        # Build list context
        from dataclasses import replace

        if context:
            list_context = replace(context, backend_path=route.backend_path)
        else:
            from nexus.core.permissions import OperationContext

            list_context = OperationContext(
                user="anonymous", groups=[], backend_path=route.backend_path
            )

        # Issue #901: Parallel directory traversal for 5-10x speedup
        all_paths = self._list_dir_parallel(
            backend=route.backend,
            root_path=path,
            backend_path=route.backend_path,
            context=list_context,
            recursive=recursive,
        )

        # Permission filtering
        if self._enforce_permissions and context:
            from nexus.core.permissions import OperationContext

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
            return self._list_connector_details(all_paths, route, path, list_context)
        return all_paths

    def _list_connector_details(
        self,
        all_paths: builtins.list[str],
        route: Any,
        path: str,
        list_context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Build detailed results for dynamic connector paths."""
        results_with_details = []
        for entry_path in all_paths:
            file_meta = self.metadata.get(entry_path)
            is_dir = (
                file_meta
                and hasattr(file_meta, "mime_type")
                and file_meta.mime_type == "inode/directory"
            )
            if not is_dir:
                try:
                    backend_relative = entry_path[len(path) :].lstrip("/")
                    is_dir = route.backend.is_directory(backend_relative, context=list_context)
                except Exception:
                    is_dir = False
            name = entry_path.rstrip("/").split("/")[-1]
            results_with_details.append(
                {
                    "path": entry_path,
                    "size": file_meta.size if file_meta and hasattr(file_meta, "size") else 0,
                    "modified_at": (
                        file_meta.updated_at.isoformat()
                        if file_meta and hasattr(file_meta, "updated_at") and file_meta.updated_at
                        else None
                    ),
                    "created_at": (
                        file_meta.created_at.isoformat()
                        if file_meta and hasattr(file_meta, "created_at") and file_meta.created_at
                        else None
                    ),
                    "etag": file_meta.etag if file_meta and hasattr(file_meta, "etag") else None,
                    "mime_type": (
                        file_meta.mime_type
                        if file_meta and hasattr(file_meta, "mime_type")
                        else None
                    ),
                    "is_directory": is_dir,
                    "name": name,
                    "type": "directory" if is_dir else "file",
                    "updated_at": (
                        file_meta.updated_at.isoformat()
                        if file_meta and hasattr(file_meta, "updated_at") and file_meta.updated_at
                        else None
                    ),
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
        from nexus.core._metadata_generated import FileMetadata

        _preapproved_dirs: set[str] = set()
        _revision_before: int | None = None

        if _rebac_manager and hasattr(_rebac_manager, "_get_zone_revision_for_grant"):
            _revision_before = _rebac_manager._get_zone_revision_for_grant(list_zone_id)

        import time as _time

        _idx_start = _time.time()
        dir_entries = self.metadata.list_directory_entries(path, zone_id=list_zone_id)
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

        all_files = []
        _perm_start = _time.time()
        for entry in dir_entries:
            entry_path = f"{path.rstrip('/')}/{entry['name']}"
            if entry["type"] == "directory":
                if self._permission_enforcer.has_accessible_descendants(entry_path, context):
                    _preapproved_dirs.add(entry_path)
                    all_files.append(
                        FileMetadata(
                            path=entry_path,
                            backend_name="",
                            physical_path="",
                            size=0,
                            created_at=entry.get("created_at"),
                            etag=None,
                            mime_type="inode/directory",
                        )
                    )
            else:
                all_files.append(
                    FileMetadata(
                        path=entry_path,
                        backend_name="",
                        physical_path="",
                        size=0,
                        created_at=entry.get("created_at"),
                        etag=None,
                        mime_type=None,
                    )
                )
        logger.info(
            f"[LIST-TIMING] has_accessible_descendants(): "
            f"{(_time.time() - _perm_start) * 1000:.1f}ms for {len(dir_entries)} entries"
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

    def _list_slow_path(
        self,
        list_prefix: str,
        list_zone_id: str,
        subject_type: str | None,
        subject_id: str | None,
        _revision_before: int | None,
        _rebac_manager: Any,
    ) -> tuple[builtins.list[Any], set[int] | None]:
        """Full recursive metadata scan with predicate pushdown optimization."""
        import os as _os
        import time as _time

        _accessible_int_ids: set[int] | None = None
        _pushdown_disabled = _os.getenv("NEXUS_DISABLE_PREDICATE_PUSHDOWN", "").lower() in (
            "1",
            "true",
        )

        if self._enforce_permissions and subject_type and subject_id and not _pushdown_disabled:
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
        all_files = self.metadata.list(
            list_prefix,
            zone_id=list_zone_id,
        )
        logger.info(
            f"[LIST-TIMING] metadata.list(): {(_time.time() - _meta_start) * 1000:.1f}ms, "
            f"{len(all_files)} files"
        )

        # Predicate pushdown: filter by accessible_int_ids at service layer
        if _accessible_int_ids is not None:
            tiger_cache = getattr(_rebac_manager, "_tiger_cache", None) if _rebac_manager else None
            if tiger_cache is not None:
                before_count = len(all_files)
                all_files = [
                    f
                    for f in all_files
                    if tiger_cache.get_or_create_int_id("file", f.path) in _accessible_int_ids
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
                    all_files = self.metadata.list(
                        list_prefix,
                        zone_id=list_zone_id,
                    )
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

        from nexus.core.permissions import OperationContext

        perm_start = time.time()
        ctx_raw = context or self._default_context
        assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
        ctx: OperationContext = ctx_raw

        candidate_paths: set[str] = set()
        candidate_paths.update(meta.path for meta in all_files)

        if not recursive:
            backend_dirs = self._get_backend_directory_entries(path)
            candidate_paths.update(backend_dirs)

        # Single permission filter call
        filter_start = time.time()
        if _accessible_int_ids is not None:
            allowed_set = {meta.path for meta in all_files}
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
        zone_id: str = "default",
    ) -> set[str]:
        """Infer directory entries from file paths and backend."""
        import time as _time

        _dir_start = _time.time()
        directories: set[str] = set()

        for meta in results:
            if meta.mime_type == "inode/directory":
                directories.add(meta.path)

        if not recursive:
            if self._enforce_permissions and context:
                for meta in all_files:
                    if meta.path in allowed_set:
                        rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
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
                    rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
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
        zone_id: str = "default",
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
                "path": meta.path,
                "size": meta.size,
                "modified_at": meta.modified_at,
                "created_at": meta.created_at,
                "etag": meta.etag,
                "mime_type": meta.mime_type,
                "is_directory": False,
            }
            for meta in results
            if meta.mime_type != "inode/directory"
        ]
        dir_results = [
            {
                "path": dir_path,
                "size": 0,
                "modified_at": None,
                "created_at": None,
                "etag": None,
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

        file_paths = [meta.path for meta in results if meta.mime_type != "inode/directory"]
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
    ) -> Any:
        """Paginated list with over-fetch strategy for permission filtering (Issue #937)."""
        from nexus.core._metadata_generated import PaginatedResult
        from nexus.core.pagination import encode_cursor

        context = context or self._default_context
        import time as _time

        _start = _time.time()

        list_zone_id, _, _ = self._extract_zone_info(context)

        if path and path != "/":
            path = self._validate_path(path)
        if path and not path.endswith("/"):
            path = path + "/"
        list_prefix = path if path != "/" else ""

        buffer_multiplier = 1.5
        fetch_limit = int(limit * buffer_multiplier)
        collected_items: builtins.list[Any] = []
        current_cursor = cursor
        has_more = True

        while len(collected_items) < limit and has_more:
            batch = self.metadata.list_paginated(
                prefix=list_prefix,
                recursive=recursive,
                limit=fetch_limit,
                cursor=current_cursor,
                zone_id=list_zone_id,
            )

            from nexus.core.nexus_fs_core import SYSTEM_PATH_PREFIX

            batch.items = [
                item for item in batch.items if not item.path.startswith(SYSTEM_PATH_PREFIX)
            ]

            if self._enforce_permissions and context:
                paths = [item.path for item in batch.items]
                allowed_paths = set(self._permission_enforcer.filter_list(paths, context))
                filtered_items = [item for item in batch.items if item.path in allowed_paths]
            else:
                filtered_items = batch.items

            collected_items.extend(filtered_items)
            has_more = batch.has_more
            current_cursor = batch.next_cursor
            if not batch.items:
                break

        result_items = collected_items[:limit]
        final_has_more = has_more or len(collected_items) > limit

        next_cursor = None
        if final_has_more and result_items:
            last_item = result_items[-1]
            filters = {"prefix": list_prefix, "recursive": recursive, "zone_id": list_zone_id}
            next_cursor = encode_cursor(
                last_path=last_item.path,
                last_path_id=None,
                filters=filters,
            )

        if details:
            items_output = [
                {
                    "path": meta.path,
                    "size": meta.size,
                    "modified_at": meta.modified_at,
                    "created_at": meta.created_at,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "is_directory": meta.is_dir if hasattr(meta, "is_dir") else False,
                }
                for meta in result_items
            ]
        else:
            items_output = [meta.path for meta in result_items]

        return PaginatedResult(
            items=items_output,
            next_cursor=next_cursor,
            has_more=final_has_more,
            total_count=None,
        )

    def _list_memory_path(
        self, path: str, details: bool = False
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List memories via virtual path (Phase 2 Integration v0.4.0)."""
        if self._gw_session_factory is None:
            logger.warning("session_factory not provided, cannot list memory paths")
            return []

        from nexus.core.memory_router import MemoryViewRouter
        from nexus.services.permissions.entity_registry import EntityRegistry

        parts = [p for p in path.split("/") if p]
        session = self._gw_session_factory()
        try:
            registry = EntityRegistry(session)
            router = MemoryViewRouter(session, registry)
            ids = registry.extract_ids_from_path_parts(parts)
            memories = router.query_memories(
                zone_id=ids.get("zone_id"),
                user_id=ids.get("user_id"),
                agent_id=ids.get("agent_id"),
            )

            if details:
                detail_results: builtins.list[dict[str, Any]] = []
                for mem in memories:
                    paths = router.get_virtual_paths(mem)
                    mem_path = paths[0] if paths else f"/objs/memory/{mem.memory_id}"
                    size = 0
                    if self._gw_backend:
                        try:
                            size = len(self._gw_backend.read_content(mem.content_hash).unwrap())
                        except Exception:
                            logger.debug("Failed to read memory content size: %s", mem.memory_id)
                    detail_results.append(
                        {
                            "path": mem_path,
                            "size": size,
                            "modified_at": mem.created_at,
                            "etag": mem.content_hash,
                        }
                    )
                return detail_results
            else:
                path_results: builtins.list[str] = []
                for mem in memories:
                    paths = router.get_virtual_paths(mem)
                    if paths:
                        path_results.append(paths[0])
                return path_results
        finally:
            session.close()

    def _get_cross_zone_shared_paths(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
        prefix: str = "",
    ) -> builtins.list[str]:
        """Fetch file paths shared with a user from other zones (Issue #904)."""
        import sqlite3
        import time as _time
        from datetime import UTC, datetime

        from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS

        if not self._rebac_manager:
            return []

        # Check TTL cache (5-second TTL)
        cache_key = (subject_type, subject_id, zone_id, prefix)
        now = _time.monotonic()
        if cache_key in self._cross_zone_cache:
            cached_time, cached_paths = self._cross_zone_cache[cache_key]
            if now - cached_time < 5.0:
                return cached_paths

        try:
            with self._rebac_manager._connection() as conn:
                cursor = self._rebac_manager._create_cursor(conn)
                cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
                placeholders = ", ".join("?" * len(cross_zone_relations))
                query = f"""
                    SELECT DISTINCT object_id
                    FROM rebac_tuples
                    WHERE relation IN ({placeholders})
                      AND subject_type = ? AND subject_id = ?
                      AND object_type = 'file'
                      AND zone_id != ?
                      AND (expires_at IS NULL OR expires_at > ?)
                """
                base_params: tuple[Any, ...] = (
                    *cross_zone_relations,
                    subject_type,
                    subject_id,
                    zone_id,
                    datetime.now(UTC).isoformat(),
                )
                if prefix:
                    query += " AND object_id LIKE ?"
                    params = (*base_params, f"{prefix}%")
                else:
                    params = base_params
                cursor.execute(self._rebac_manager._fix_sql_placeholders(query), params)
                paths = []
                for row in cursor.fetchall():
                    path = row["object_id"] if isinstance(row, dict) else row[0]
                    paths.append(path)
                if paths:
                    logger.debug(
                        f"[CROSS-ZONE] Found {len(paths)} shared paths "
                        f"for {subject_type}:{subject_id}"
                    )
                self._cross_zone_cache[cache_key] = (now, paths)
                return paths
        except (sqlite3.OperationalError, sqlite3.InterfaceError) as e:
            logger.error("Cross-zone sharing DB error for %s/%s: %s", subject_type, subject_id, e)
            return []
        except Exception as e:
            logger.error("Unexpected cross-zone sharing error: %s", e, exc_info=True)
            return []

    # =========================================================================
    # Public API: Glob Pattern Matching
    # =========================================================================

    @rpc_expose(description="Find files by glob pattern")
    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern.

        Supports *, **, ?, [...] patterns. Issue #538: Automatically excludes
        gitignore-style patterns. Results sorted by mtime (newest first).

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "data/*.csv")
            path: Base path to search from (default: "/")
            context: Operation context for permission filtering
        """
        if path and path != "/":
            path = self._validate_path(path)

        import time

        glob_start = time.time()

        # Phase 1: Directory-level pruning (Issue #929: DIRECTORY_PRUNED strategy)
        search_path = path
        if path == "/" or path == "":
            static_prefix = glob_fast.extract_static_prefix(pattern)
            if static_prefix:
                search_path = (
                    static_prefix.rstrip("/")
                    if static_prefix.startswith("/")
                    else "/" + static_prefix.rstrip("/")
                )

        # Phase 2: Get accessible files
        list_start = time.time()
        accessible_files: list[str] = cast(
            list[str], self.list(search_path, recursive=True, context=context)
        )
        logger.debug(
            f"[GLOB] Phase 2: list() found {len(accessible_files)} files "
            f"in {time.time() - list_start:.3f}s"
        )
        if not accessible_files:
            return []

        # Phase 2.5: Gitignore filtering (Issue #538)
        pre_filter_count = len(accessible_files)
        accessible_files = _filter_ignored_paths(accessible_files)
        if pre_filter_count != len(accessible_files):
            logger.debug(
                f"[GLOB] Issue #538: Filtered {pre_filter_count - len(accessible_files)} paths"
            )

        # Phase 3: Strategy selection (Issue #929)
        strategy = self._select_glob_strategy(pattern, len(accessible_files))

        # Build full pattern
        if not path.endswith("/"):
            path = path + "/"
        if path == "/":
            full_pattern = pattern
            if (
                "**" not in full_pattern
                and not full_pattern.startswith("/")
                and not full_pattern.startswith(
                    ("workspace/", "shared/", "external/")
                )  # Issue #1572
                and "/" in full_pattern
            ):
                full_pattern = "**/" + full_pattern
        else:
            base_path = path[1:] if path.startswith("/") else path
            full_pattern = base_path + pattern

        # Phase 4: Execute strategy-specific matching
        match_start = time.time()
        matches: list[str] = []

        if strategy in (GlobStrategy.RUST_BULK, GlobStrategy.DIRECTORY_PRUNED):
            rust_pattern = full_pattern if full_pattern.startswith("/") else "/" + full_pattern
            rust_matches = glob_fast.glob_match_bulk([rust_pattern], accessible_files)
            if rust_matches is not None:
                matches = rust_matches
            else:
                strategy = (
                    GlobStrategy.REGEX_COMPILED
                    if "**" in full_pattern
                    else GlobStrategy.FNMATCH_SIMPLE
                )

        if strategy == GlobStrategy.REGEX_COMPILED and not matches:
            matches = self._glob_regex_match(full_pattern, accessible_files)

        if strategy == GlobStrategy.FNMATCH_SIMPLE and not matches:
            for file_path in accessible_files:
                path_for_match = file_path[1:] if file_path.startswith("/") else file_path
                if fnmatch.fnmatch(path_for_match, full_pattern):
                    matches.append(file_path)

        logger.debug(
            f"[GLOB] {strategy.value}: matched {len(matches)}/{len(accessible_files)} files "
            f"in {time.time() - match_start:.3f}s (total: {time.time() - glob_start:.3f}s)"
        )

        # Sort by mtime (newest first) (Issue #538)
        if matches:
            try:
                metadata_map = self.metadata.get_file_metadata_bulk(matches, "modified_at")
                return sorted(
                    matches,
                    key=lambda p: (-(metadata_map.get(p, 0) or 0), p),
                )
            except Exception as e:
                logger.debug(f"[GLOB] mtime sort failed ({e}), falling back to alphabetical")
                return sorted(matches)
        return []

    def _glob_regex_match(
        self, full_pattern: str, accessible_files: builtins.list[str]
    ) -> builtins.list[str]:
        """Match files using compiled regex for ** patterns."""
        parts = full_pattern.split("**")
        regex_parts = []
        for i, part in enumerate(parts):
            if i > 0:
                regex_parts.append("(?:.*/)?")
            escaped = re.escape(part)
            escaped = escaped.replace(r"\*", "[^/]*")
            escaped = escaped.replace(r"\?", ".")
            escaped = escaped.replace(r"\[", "[").replace(r"\]", "]")
            while escaped.startswith("/"):
                escaped = escaped[1:]
            regex_parts.append(escaped)
        regex_pattern = "^/" + "".join(regex_parts) + "$"
        compiled_regex = re.compile(regex_pattern)
        return [fp for fp in accessible_files if compiled_regex.match(fp)]

    @rpc_expose(description="Execute multiple glob patterns in single call")
    def glob_batch(
        self, patterns: builtins.list[str], path: str = "/", context: Any = None
    ) -> dict[str, builtins.list[str]]:
        """Execute multiple glob patterns in a single call (Issue #859).

        Shares file listing across all patterns for major optimization.

        Args:
            patterns: List of glob patterns to match
            path: Base path to search from (default: "/")
            context: Operation context for permission filtering
        """
        results: dict[str, list[str]] = {}
        try:
            if path and path != "/":
                path = self._validate_path(path)
            accessible_files: builtins.list[str] = cast(
                builtins.list[str], self.list(path, recursive=True, context=context)
            )
        except Exception:
            for pattern in patterns:
                results[pattern] = []
            return results

        for pattern in patterns:
            try:
                search_path = path
                if not search_path.endswith("/"):
                    search_path = search_path + "/"
                if search_path == "/":
                    full_pattern = pattern
                    if (
                        "**" not in full_pattern
                        and not full_pattern.startswith("/")
                        and not full_pattern.startswith(
                            ("workspace/", "shared/", "external/")
                        )  # Issue #1572
                        and "/" in full_pattern
                    ):
                        full_pattern = "**/" + full_pattern
                else:
                    base_path = search_path[1:] if search_path.startswith("/") else search_path
                    full_pattern = base_path + pattern

                rust_pattern = full_pattern if full_pattern.startswith("/") else "/" + full_pattern
                rust_matches = glob_fast.glob_match_bulk([rust_pattern], accessible_files)
                if rust_matches is not None:
                    results[pattern] = sorted(rust_matches)
                elif "**" in full_pattern:
                    results[pattern] = sorted(
                        self._glob_regex_match(full_pattern, accessible_files)
                    )
                else:
                    matches = []
                    for file_path in accessible_files:
                        path_for_match = file_path[1:] if file_path.startswith("/") else file_path
                        if fnmatch.fnmatch(path_for_match, full_pattern):
                            matches.append(file_path)
                    results[pattern] = sorted(matches)
            except Exception:
                logger.debug("glob_batch pattern failed: %s", pattern, exc_info=True)
                results[pattern] = []
        return results

    # =========================================================================
    # Public API: Content Searching (Grep)
    # =========================================================================

    @rpc_expose(description="Search file contents")
    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",  # noqa: ARG002
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        r"""Search file contents using regex patterns.

        Uses adaptive algorithm selection (Issue #929) with 5 strategies.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, case-insensitive search
            max_results: Maximum number of results (default: 100)
            search_mode: Deprecated, kept for backward compat
            context: Operation context for permission filtering
        """
        if path and path != "/":
            path = self._validate_path(path)

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Phase 1: Get files to search
        if file_pattern:
            files = self.glob(file_pattern, path, context=context)
        else:
            files = cast(list[str], self.list(path, recursive=True, context=context))
            pre_filter_count = len(files)
            files = _filter_ignored_paths(files)
            if pre_filter_count != len(files):
                logger.debug(f"[GREP] Issue #538: Filtered {pre_filter_count - len(files)} paths")

        if not files:
            return []

        # Phase 2: Bulk fetch searchable text
        searchable_texts = self.metadata.get_searchable_text_bulk(files)
        cached_text_ratio = len(searchable_texts) / len(files) if files else 0.0
        files_needing_raw = [f for f in files if f not in searchable_texts]

        # Phase 3: Select strategy (Issue #929)
        strategy = self._select_grep_strategy(
            file_count=len(files),
            cached_text_ratio=cached_text_ratio,
        )

        # Phase 4: Execute strategy-specific search
        results: list[dict[str, Any]] = []

        # Strategy: ZOEKT_INDEX
        if strategy == SearchStrategy.ZOEKT_INDEX:
            zoekt_results = self._try_grep_with_zoekt(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=max_results,
                context=context,
            )
            if zoekt_results is not None:
                return zoekt_results
            strategy = SearchStrategy.RUST_BULK

        # Strategy: CACHED_TEXT or opportunistic cached text search
        if strategy == SearchStrategy.CACHED_TEXT or searchable_texts:
            for file_path, text in searchable_texts.items():
                if len(results) >= max_results:
                    break
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break
                    match_obj = regex.search(line)
                    if match_obj:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match_obj.group(0),
                            }
                        )
            if strategy == SearchStrategy.CACHED_TEXT and len(results) >= max_results:
                return results[:max_results]

        if len(results) >= max_results:
            return results[:max_results]

        # Process remaining files needing raw content
        if not files_needing_raw:
            return results

        remaining_results = max_results - len(results)

        if strategy == SearchStrategy.PARALLEL_POOL:
            results.extend(
                self._grep_parallel(
                    regex=regex,
                    files=files_needing_raw,
                    max_results=remaining_results,
                    context=context,
                )
            )
        elif strategy in (SearchStrategy.RUST_BULK, SearchStrategy.SEQUENTIAL):
            results.extend(
                self._grep_raw_content(
                    regex=regex,
                    pattern=pattern,
                    files_needing_raw=files_needing_raw,
                    strategy=strategy,
                    ignore_case=ignore_case,
                    remaining_results=remaining_results,
                    context=context,
                )
            )

        return results[:max_results]

    # =========================================================================
    # Grep Helpers
    # =========================================================================

    def _grep_raw_content(
        self,
        regex: re.Pattern[str],
        pattern: str,
        files_needing_raw: builtins.list[str],
        strategy: SearchStrategy,
        ignore_case: bool,
        remaining_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Process files needing raw content read (mmap, Rust bulk, sequential)."""
        results: builtins.list[dict[str, Any]] = []
        mmap_used = False

        # Try mmap-accelerated grep first (Issue #893)
        if grep_fast.is_mmap_available():
            try:
                from nexus.storage.file_cache import get_file_cache

                zone_id, _, _ = self._extract_zone_info(context)
                if zone_id:
                    file_cache = get_file_cache()
                    disk_paths = file_cache.get_disk_paths_bulk(zone_id, files_needing_raw)
                    if disk_paths:
                        disk_to_virtual = {dp: vp for vp, dp in disk_paths.items()}
                        mmap_results = grep_fast.grep_files_mmap(
                            pattern,
                            list(disk_paths.values()),
                            ignore_case=ignore_case,
                            max_results=remaining_results,
                        )
                        if mmap_results is not None:
                            for match in mmap_results:
                                disk_path = match.get("file", "")
                                match["file"] = disk_to_virtual.get(disk_path, disk_path)
                            results.extend(mmap_results)
                            mmap_used = True
                            files_needing_raw = [
                                f for f in files_needing_raw if f not in disk_paths
                            ]
                            remaining_results = remaining_results - len(results)
            except Exception as e:
                logger.debug(f"[GREP] Mmap optimization failed: {e}")

        # Rust-accelerated grep for remaining
        if (
            strategy == SearchStrategy.RUST_BULK
            and grep_fast.is_available()
            and remaining_results > 0
            and files_needing_raw
        ):
            bulk_results = self._read_bulk(files_needing_raw, context=context, skip_errors=True)
            file_contents: dict[str, bytes] = {
                fp: content
                for fp, content in bulk_results.items()
                if content is not None and isinstance(content, bytes)
            }
            rust_results = grep_fast.grep_bulk(
                pattern,
                file_contents,
                ignore_case=ignore_case,
                max_results=remaining_results,
            )
            if rust_results is not None:
                results.extend(rust_results)

        # Python sequential fallback
        elif not mmap_used and files_needing_raw:
            for file_path in files_needing_raw:
                if len(results) >= remaining_results:
                    break
                try:
                    read_result = self._read(file_path, context=context)
                    if not isinstance(read_result, bytes):
                        continue
                    try:
                        text = read_result.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    for line_num, line in enumerate(text.splitlines(), start=1):
                        if len(results) >= remaining_results:
                            break
                        match_obj = regex.search(line)
                        if match_obj:
                            results.append(
                                {
                                    "file": file_path,
                                    "line": line_num,
                                    "content": line,
                                    "match": match_obj.group(0),
                                }
                            )
                except Exception:
                    continue

        return results

    def _try_grep_with_zoekt(
        self,
        pattern: str,
        path: str,
        file_pattern: str | None,
        ignore_case: bool,
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]] | None:
        """Try Zoekt for accelerated grep. Returns None if not available."""
        try:
            from nexus.search.zoekt_client import get_zoekt_client
        except ImportError:
            return None

        client = get_zoekt_client()
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return None
            is_available = loop.run_until_complete(client.is_available())
        except RuntimeError:
            is_available = asyncio.run(client.is_available())

        if not is_available:
            return None

        try:
            zoekt_query = pattern
            if ignore_case:
                zoekt_query = f"(?i){pattern}"
            if path and path != "/":
                zoekt_query = f"file:{path.lstrip('/')}/ {zoekt_query}"

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return None
                matches = loop.run_until_complete(client.search(zoekt_query, num=max_results * 3))
            except RuntimeError:
                matches = asyncio.run(client.search(zoekt_query, num=max_results * 3))

            if not matches:
                return None

            if file_pattern:
                matches = [m for m in matches if glob_fast.glob_match(m.file, [file_pattern])]

            unique_files = list({m.file for m in matches})
            if self._permission_enforcer and context:
                permitted_files = set(self._permission_enforcer.filter_list(unique_files, context))
            else:
                permitted_files = set(unique_files)

            results = []
            for match in matches:
                if match.file in permitted_files:
                    results.append(
                        {
                            "file": match.file,
                            "line": match.line,
                            "content": match.content,
                            "match": match.match,
                        }
                    )
                    if len(results) >= max_results:
                        break
            return results
        except Exception as e:
            logger.warning(f"[GREP] Zoekt search failed: {e}")
            return None

    def _grep_parallel(
        self,
        regex: re.Pattern[str],
        files: builtins.list[str],
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Parallel grep using ThreadPoolExecutor (Issue #929).

        Each worker searches its chunk independently. Results are merged and
        truncated to ``max_results`` in the main thread.
        """
        from nexus.utils.timing import Timer

        timer = Timer()
        timer.__enter__()

        chunk_size = max(1, len(files) // GREP_PARALLEL_WORKERS)
        file_chunks = [files[i : i + chunk_size] for i in range(0, len(files), chunk_size)]

        def search_chunk(chunk_files: builtins.list[str]) -> builtins.list[dict[str, Any]]:
            chunk_results: builtins.list[dict[str, Any]] = []
            for file_path in chunk_files:
                try:
                    read_result = self._read(file_path, context=context)
                    if not isinstance(read_result, bytes):
                        continue
                    try:
                        text = read_result.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    for line_num, line in enumerate(text.splitlines(), start=1):
                        match_obj = regex.search(line)
                        if match_obj:
                            chunk_results.append(
                                {
                                    "file": file_path,
                                    "line": line_num,
                                    "content": line,
                                    "match": match_obj.group(0),
                                }
                            )
                            if len(chunk_results) >= max_results:
                                break
                except Exception:
                    continue
            return chunk_results

        all_results: builtins.list[dict[str, Any]] = []
        executor = self._get_thread_pool()
        futures = [executor.submit(search_chunk, chunk) for chunk in file_chunks]
        for future in futures:
            try:
                chunk_results = future.result(timeout=30)
                all_results.extend(chunk_results)
                if len(all_results) >= max_results:
                    break
            except Exception as e:
                logger.debug(f"[GREP-PARALLEL] Chunk failed: {e}")

        timer.__exit__(None, None, None)
        logger.debug(
            f"[GREP-PARALLEL] {len(files)} files, {len(all_results)} results "
            f"in {timer.elapsed:.3f}s"
        )
        return all_results[:max_results]

    # =========================================================================
    # Algorithm Selection (Issue #929)
    # =========================================================================

    def _is_zoekt_available(self) -> bool:
        """Check if Zoekt indexing service is available."""
        try:
            from nexus.search.zoekt_client import get_zoekt_client

            client = get_zoekt_client()
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return False
                return loop.run_until_complete(client.is_available())
            except RuntimeError:
                return asyncio.run(client.is_available())
        except (ImportError, Exception):
            return False

    def _select_grep_strategy(
        self,
        file_count: int,
        cached_text_ratio: float,
        zoekt_available: bool | None = None,
    ) -> SearchStrategy:
        """Select optimal grep strategy (Issue #929)."""
        if cached_text_ratio >= GREP_CACHED_TEXT_RATIO:
            return SearchStrategy.CACHED_TEXT
        if file_count < GREP_SEQUENTIAL_THRESHOLD:
            return SearchStrategy.SEQUENTIAL
        if file_count > GREP_ZOEKT_THRESHOLD:
            if zoekt_available is None:
                zoekt_available = self._is_zoekt_available()
            if zoekt_available:
                return SearchStrategy.ZOEKT_INDEX
        if GREP_PARALLEL_THRESHOLD <= file_count <= 10000:
            return SearchStrategy.PARALLEL_POOL
        if grep_fast.is_available():
            return SearchStrategy.RUST_BULK
        return SearchStrategy.SEQUENTIAL

    def _select_glob_strategy(self, pattern: str, file_count: int) -> GlobStrategy:
        """Select optimal glob strategy (Issue #929)."""
        static_prefix = glob_fast.extract_static_prefix(pattern)
        if static_prefix:
            return GlobStrategy.DIRECTORY_PRUNED
        if file_count > GLOB_RUST_THRESHOLD and glob_fast.is_available():
            return GlobStrategy.RUST_BULK
        if "**" in pattern:
            return GlobStrategy.REGEX_COMPILED
        return GlobStrategy.FNMATCH_SIMPLE

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
        from nexus.core.permissions import OperationContext

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
