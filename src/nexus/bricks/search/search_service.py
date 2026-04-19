"""Search Service - Extracted from NexusFSSearchMixin (Issue #1287).

This service handles all search operations:
- File listing with pagination and permission filtering
- Glob pattern matching with adaptive algorithms
- Content searching (grep) with 5 strategies
- Semantic search with embeddings

Extracted from: nexus_fs_search.py (2,817 lines)
"""

import asyncio
import builtins
import fnmatch
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, cast

from cachetools import TTLCache

from nexus.bricks.search.primitives import glob_fast, grep_fast, trigram_fast
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import PermissionDeniedError
from nexus.contracts.search_types import (
    GLOB_RUST_THRESHOLD,
    GREP_CACHED_TEXT_RATIO,
    GREP_PARALLEL_THRESHOLD,
    GREP_PARALLEL_WORKERS,
    GREP_SEQUENTIAL_THRESHOLD,
    GREP_TRIGRAM_THRESHOLD,
    GREP_ZOEKT_THRESHOLD,
    LAST_SEMANTIC_DEGRADED,
    GlobStrategy,
    SearchStrategy,
)
from nexus.contracts.types import Permission
from nexus.lib.rpc_decorator import rpc_expose

# List directory traversal thresholds (Issue #901)
# Issue #2071: LIST_PARALLEL_WORKERS now sourced from ProfileTuning.search.list_parallel_workers
# Kept as fallback for callers that don't receive tuning via DI.
LIST_PARALLEL_WORKERS = 10  # Thread pool size for parallel directory listing (FULL profile default)
LIST_PARALLEL_MAX_DEPTH = 100  # Safety limit to prevent infinite traversal (e.g., symlink loops)

# Issue #3701 (2A + 7A): hard cap on caller-supplied `files=[...]` list
# size. Beyond this we reject with ValueError rather than silently
# truncate — the whole point of the param is for an agent to pass the
# narrowed working set it already has, and anything past this is almost
# certainly an abuse of the parameter.
FILES_FILTER_SIZE_CAP = 10_000

# Issue #3701 (2A + 13A): when `files=[...]` is set, this threshold
# decides between "grep the files directly" (O(files)) and "use the
# zone trigram index then post-filter" (O(matches)). Below the threshold
# we bypass trigram; above it we use trigram. Benchmark-backed in
# tests/benchmarks/test_search_benchmarks.py::TestFilesFilterThreshold.
FILES_FILTER_TRIGRAM_THRESHOLD = 200

# Issue #3701 (13A): when trigram runs with a files filter we over-fetch
# so post-filter truncation doesn't leave the caller short. 3x matches
# the ReBAC over-fetch factor — adjust if benchmarks show a better value.
_REBAC_OVERFETCH_FACTOR_FOR_FILES = 3

# Issue #3720: block_type filter for markdown grep.  When set, grep
# over-fetches internally (most matches may be in prose) then post-filters
# to lines inside the requested block type's line ranges.
VALID_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "code",
        "table",
        "frontmatter",
        "paragraph",
        "blockquote",
        "list",
        "heading",
    }
)
# Issue #3720 (Codex R4): markdown file extensions for block_type filtering.
_MARKDOWN_EXTENSIONS: tuple[str, ...] = (".md", ".markdown", ".mdown", ".mkd")
_BLOCK_TYPE_OVERFETCH_FACTOR = 5
_BLOCK_TYPE_OVERFETCH_CAP = 2000

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
    from nexus.bricks.rebac.enforcer import PermissionEnforcer
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.bricks.search.indexing_service import IndexingService
    from nexus.bricks.search.pipeline_indexer import PipelineIndexer
    from nexus.bricks.search.query_service import QueryService
    from nexus.contracts.types import OperationContext
    from nexus.core.metastore import MetastoreABC
    from nexus.core.router import PathRouter
    from nexus.services.gateway import NexusFSGateway


def _result_to_dict(r: Any) -> dict[str, Any]:
    """Convert a BaseSearchResult to a canonical dict."""
    return {
        "path": r.path,
        "chunk_index": r.chunk_index,
        "chunk_text": r.chunk_text,
        "score": r.score,
        "start_offset": r.start_offset,
        "end_offset": r.end_offset,
        "line_start": r.line_start,
        "line_end": r.line_end,
    }


class SearchService:
    """Independent search service extracted from NexusFS.

    Handles file listing, glob matching, grep, and semantic search.
    Semantic search methods (formerly in SemanticSearchMixin) are inlined.

    Uses adaptive algorithm selection (Issue #929) to choose optimal
    strategies based on data characteristics. No direct filesystem
    dependencies; uses dependency injection for stores and backends.
    """

    def __init__(
        self,
        metadata_store: "MetastoreABC",
        permission_enforcer: "PermissionEnforcer | None" = None,
        router: "PathRouter | None" = None,
        rebac_manager: "ReBACManager | None" = None,
        enforce_permissions: bool = True,
        default_context: "OperationContext | None" = None,
        record_store: Any | None = None,
        # Gateway for NexusFS operations (Issue #1287, replaces 8 Callable params)
        gateway: "NexusFSGateway | None" = None,
        list_parallel_workers: int = LIST_PARALLEL_WORKERS,
        grep_parallel_workers: int = GREP_PARALLEL_WORKERS,
        file_cache: Any | None = None,
        zoekt_client: Any | None = None,
        deployment_profile: str | None = None,
        sqlite_vec_backend: Any | None = None,
        federation_dispatcher: Any | None = None,
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
            zoekt_client: Injected ZoektClient instance (Issue #2188).
            deployment_profile: Active deployment profile name (Issue #3778).
                When set to ``"sandbox"``, a semantic search that goes through
                ``_semantic_with_sandbox_fallback`` will degrade gracefully to
                local BM25S when federation reports all peers unreachable.
            sqlite_vec_backend: Optional ``SqliteVecBackend`` instance
                (Issue #3778). When supplied, SANDBOX-profile semantic search
                tries this local vector backend first; a non-empty result set
                short-circuits the federation/BM25S fallback chain. The
                factory only wires this when ``profile=sandbox`` AND
                ``cfg.enable_vector_search`` AND both ``sqlite-vec`` +
                ``litellm`` are importable.
        """
        self.metadata = metadata_store
        self._record_store = record_store
        self._fp_engine: Any = None  # Issue #3266: cached SQLAlchemy engine
        # Injected file cache (Issue #690 — replaces global singleton)
        self._file_cache = file_cache
        self._zoekt_client = zoekt_client
        self._permission_enforcer = permission_enforcer
        self.router = router
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._default_context = default_context

        # Gateway for NexusFS operations (Issue #1287)
        self._gw = gateway

        # Semantic search (initialized later via ainitialize_semantic_search)
        self._query_service: QueryService | None = None
        self._indexing_service: IndexingService | None = None
        self._indexing_pipeline: Any = None
        self._pipeline_indexer: PipelineIndexer | None = None

        # Shared thread pool for parallel grep (Issue #929, fix #14)
        self._thread_pool: ThreadPoolExecutor | None = None
        self._grep_parallel_workers = grep_parallel_workers

        # Shared thread pool for parallel directory listing (Issue #899)
        self._list_thread_pool: ThreadPoolExecutor | None = None
        self._list_parallel_workers = list_parallel_workers

        # Lock for lazy thread pool initialization (prevents TOCTOU race)
        self._pool_lock = threading.Lock()

        # Bounded TTL cache for cross-zone sharing queries (Issue #904)
        self._cross_zone_cache: TTLCache[tuple[str, ...], builtins.list[str]] = TTLCache(
            maxsize=1024, ttl=5.0
        )

        # Issue #3778: SANDBOX profile — degrade semantic search to BM25S when
        # federation reports all peers unreachable. The warn-once flag lives on
        # the instance so a long-running sandbox doesn't spam the log.
        self._deployment_profile = (deployment_profile or "").lower() or None
        self._sandbox_fallback_warned = False
        # Issue #3778: optional local vector backend (sqlite-vec + litellm).
        # When non-None on SANDBOX, semantic search tries the local backend
        # first and only falls back to federation/BM25S when it returns
        # empty (or raises).
        self._sqlite_vec_backend = sqlite_vec_backend

        # Issue #3778 (R1 review): optional real federation dispatcher. When
        # set, SANDBOX semantic fallback routes through it instead of
        # fabricating an empty "no-peers" FederatedSearchResponse — so if a
        # future deployment wires a dispatcher into a sandbox-profile server
        # the real federation attempt is made before BM25 degradation.
        self._federation_dispatcher = federation_dispatcher

        logger.info("[SearchService] Initialized")

    def _get_thread_pool(self) -> ThreadPoolExecutor:
        """Get or create the shared thread pool for parallel grep operations."""
        if self._thread_pool is None:
            with self._pool_lock:
                if self._thread_pool is None:
                    self._thread_pool = ThreadPoolExecutor(
                        max_workers=self._grep_parallel_workers,
                        thread_name_prefix="nexus-search",
                    )
        return self._thread_pool

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

    def _get_namespace_prefixes(self) -> tuple[str, ...]:
        """Get known mount-point prefixes from router (dynamic) or fallback (static).

        Uses ``get_mount_points()`` to derive top-level prefixes from active
        mounts.  Falls back to hardcoded defaults when no router is available.
        """
        if self.router and hasattr(self.router, "get_mount_points"):
            try:
                mount_points = self.router.get_mount_points()
                # Extract top-level segments from mount points (e.g. "/workspace" -> "workspace/")
                prefixes: set[str] = set()
                for mp in mount_points:
                    top = mp.lstrip("/").split("/")[0]
                    if top:
                        prefixes.add(f"{top}/")
                if prefixes:
                    return tuple(sorted(prefixes))
            except Exception as e:
                logger.debug("Mount prefix detection failed, using defaults: %s", e)
        return ("workspace/", "shared/", "external/", "system/", "archives/")

    def _should_prepend_recursive_wildcard(self, pattern: str) -> bool:
        """Check if glob pattern needs **/ prefix for implicit recursive search.

        Returns True when the pattern looks like a relative multi-level path
        (e.g., "models/file.py") that should match anywhere in the tree.
        Returns False when the pattern already specifies a root namespace
        (e.g., "workspace/file.py") or is already recursive/absolute.
        """
        if "**" in pattern or pattern.startswith("/") or "/" not in pattern:
            return False
        return not pattern.startswith(self._get_namespace_prefixes())

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

    async def _read(
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
        # Check if path routes to a dynamic API-backed connector
        if path and path != "/" and self.router:
            try:
                zone_id, _agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path,
                    is_admin=is_admin,
                    check_write=False,
                )
                from nexus.core.router import ExternalRouteResult

                if isinstance(route, ExternalRouteResult):
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

        import time as _time

        _list_start = _time.time()
        _preapproved_dirs: set[str] = set()
        _accessible_int_ids: set[int] | None = None

        if path and path != "/":
            path = self._validate_path(path)
        if path and not path.endswith("/"):
            path = path + "/"
        list_prefix = path if path != "/" else ""

        # Zone-scope the list prefix when called from internal methods (e.g., glob)
        # that bypass the RPC layer's _scope_params_for_zone path prefixing.
        if list_zone_id and list_zone_id != ROOT_ZONE_ID:
            zone_scope = f"/zone/{list_zone_id}"
            if list_prefix and not list_prefix.startswith(zone_scope):
                list_prefix = f"{zone_scope}{list_prefix}"

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
        from nexus.contracts.constants import SYSTEM_PATH_PREFIX

        all_files = [m for m in all_files if not m.path.startswith(SYSTEM_PATH_PREFIX)]

        # Apply recursive filter
        if recursive:
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

        # Issue #3728: virtual ``.readme/`` overlay short-circuit.
        # When the requested path is under a skill backend's virtual
        # ``.readme/`` subtree, the real backend has nothing at that
        # path and ``_list_from_metastore_or_api`` below would raise
        # ``NexusFileNotFoundError``.  Serve from the overlay first
        # and only fall through to the real backend when the overlay
        # doesn't own the path.
        virtual_replacement = self._list_virtual_readme_children(
            route=route,
            path=path,
            context=list_context,
        )
        if virtual_replacement is not None:
            all_paths = virtual_replacement
        else:
            # Issue #3266: Metastore-first listing.
            # Prefer metastore entries when available (populated by sync
            # infrastructure).  Fall back to live API on cache miss.
            try:
                all_paths = self._list_from_metastore_or_api(
                    path=path,
                    route=route,
                    list_context=list_context,
                    recursive=recursive,
                )
            except Exception as _list_exc:
                # Real listing failed — continue with an empty list so
                # the virtual overlay below can still contribute the
                # mount-root ``.readme/`` entry or descendants.
                logger.debug(
                    "[SKILL-LIST] Real backend list_dir failed for %s: %s",
                    path,
                    _list_exc,
                )
                all_paths = []

            # Issue #3728: mount-root injection.
            # On a mount-root listing, append the virtual ``.readme/``
            # subtree alongside the real backend entries.  Non-root
            # paths fall through unchanged.
            all_paths = self._augment_with_virtual_readme_entries(
                all_paths=all_paths,
                route=route,
                path=path,
                recursive=recursive,
                context=list_context,
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
            return self._list_connector_details(all_paths, route, path, list_context)
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

    def _list_virtual_readme_children(
        self,
        *,
        route: Any,
        path: str,
        context: Any,
    ) -> builtins.list[str] | None:
        """Return virtual ``.readme/`` children for a direct subtree listing.

        Issue #3728: when the caller is listing a path INSIDE the
        virtual ``.readme/`` overlay (e.g. ``/mount/.readme/`` or
        ``/mount/.readme/schemas/``), the real backend has no entries
        at that path and ``backend.list_dir`` would raise — so we
        short-circuit with the virtual tree's view before the real
        listing runs.

        Returns a fully-qualified list of child paths, or ``None``
        when the overlay does not own the path (caller continues
        with the normal real-backend listing).
        """
        backend = getattr(route, "backend", None)
        backend_path = getattr(route, "backend_path", "") or ""
        mount_point = getattr(route, "mount_point", "") or ""
        if backend is None:
            return None
        try:
            from nexus.backends.connectors.schema_generator import (
                dispatch_virtual_readme_list,
            )
        except ImportError:
            return None
        try:
            virtual_children = dispatch_virtual_readme_list(
                backend, mount_point, backend_path, context=context
            )
        except Exception:
            return None
        if virtual_children is None:
            return None

        mount_prefix = mount_point.rstrip("/")
        base = path.rstrip("/") or mount_prefix

        def _as_absolute(name: str) -> str:
            if name.startswith("/"):
                return name
            return f"{base}/{name}"

        return [_as_absolute(n) for n in virtual_children]

    def _augment_with_virtual_readme_entries(
        self,
        *,
        all_paths: builtins.list[str],
        route: Any,
        path: str,
        recursive: bool,
        context: Any,
    ) -> builtins.list[str]:
        """Inject virtual ``.readme/`` overlay entries into a connector listing.

        Issue #3728: ``_list_dynamic_connector`` calls ``backend.list_dir``
        directly, which doesn't know about the virtual tree that
        ``sys_readdir``'s ExternalRouteResult branch dispatches.  This
        helper mirrors that logic at the SearchService layer so the
        full-stack list API sees the same entries the slim CLI sees.

        Behavior:
        - Mount root, non-recursive → append ``<mount>/.readme/``
        - Mount root, recursive → flatten the virtual tree and append
          every child (``.readme/README.md``, ``.readme/schemas/...``)
        - Under ``.readme/`` → replace with the virtual tree's view of
          the requested subdirectory
        - Overlay doesn't own the subtree (deferring backend with real
          data) → no-op, return input unchanged
        """
        backend = getattr(route, "backend", None)
        backend_path = getattr(route, "backend_path", "") or ""
        mount_point = getattr(route, "mount_point", "") or ""
        if backend is None:
            return all_paths

        try:
            from nexus.backends.connectors.schema_generator import (
                _has_skill_name,
                _readme_dir_for,
                dispatch_virtual_readme_list,
                get_virtual_readme_tree_for_backend,
                overlay_owns_path,
            )
        except ImportError:
            return all_paths

        if not _has_skill_name(backend):
            return all_paths

        # Case 1: direct listing of a virtual .readme/ path — ask the
        # dispatch helper for the child names.
        try:
            virtual_children = dispatch_virtual_readme_list(
                backend, mount_point, backend_path, context=context
            )
        except Exception:
            virtual_children = None

        mount_prefix = mount_point.rstrip("/")

        def _as_absolute(name: str) -> str:
            """Turn a child name (``README.md`` or ``schemas/``) into an
            absolute virtual path like ``/mount/.readme/README.md``."""
            if name.startswith("/"):
                return name
            base = path.rstrip("/")
            return f"{base}/{name}"

        if virtual_children is not None:
            # Replace — the virtual tree is authoritative for this path.
            return [_as_absolute(name) for name in virtual_children]

        # Case 2: mount-root listing — inject the ``.readme/`` subtree
        # alongside the real backend entries.
        if backend_path.strip("/"):
            # Non-root under the mount and not under ``.readme/`` — no injection.
            return all_paths

        readme_dir_name = _readme_dir_for(backend).strip("/")
        try:
            if not overlay_owns_path(backend, mount_point, readme_dir_name, context=context):
                return all_paths
        except ValueError:
            return all_paths
        except Exception:
            return all_paths

        existing = set(all_paths)

        if not recursive:
            virtual_entry = f"{mount_prefix}/{readme_dir_name}/"
            if virtual_entry not in existing:
                return [*all_paths, virtual_entry]
            return all_paths

        # Recursive: flatten the virtual tree to every leaf + intermediate dir.
        try:
            tree = get_virtual_readme_tree_for_backend(backend, mount_point)
        except Exception:
            return all_paths

        def _walk(node: Any, prefix: str) -> builtins.list[str]:
            out: builtins.list[str] = []
            if node.is_dir:
                if prefix:
                    out.append(f"{mount_prefix}/{prefix}/")
                for child_name, child in node.children.items():
                    child_prefix = f"{prefix}/{child_name}" if prefix else child_name
                    out.extend(_walk(child, child_prefix))
            else:
                out.append(f"{mount_prefix}/{prefix}")
            return out

        flattened = _walk(tree, readme_dir_name)
        merged = list(all_paths)
        for rel in flattened:
            if rel not in existing:
                merged.append(rel)
        return merged

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
                except Exception as e:
                    logger.debug("Failed to check if %s is a directory: %s", entry_path, e)
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
        from nexus.contracts.metadata import FileMetadata

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
        all_files = self.metadata.list(
            list_prefix,
            zone_id=list_zone_id,
        )
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
        # The correct fix is to scope them: any FileMetadata whose path does
        # not start with ``/`` is a synthetic/pseudo-path that should never
        # enter the filesystem filter pipeline.
        _pre_synthetic = len(all_files)
        all_files = [f for f in all_files if f.path.startswith("/")]
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
                    if tiger_cache._resource_map.get_or_create_int_id("file", f.path)
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
                    all_files = self.metadata.list(
                        list_prefix,
                        zone_id=list_zone_id,
                    )
                    # Fix nexi-lab/nexus#3733 Bug B: same synthetic-entry
                    # guard as the primary list path above.
                    all_files = [f for f in all_files if f.path.startswith("/")]
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
        zone_id: str = ROOT_ZONE_ID,
    ) -> set[str]:
        """Infer directory entries from file paths and backend."""
        import time as _time

        _dir_start = _time.time()
        directories: set[str] = set()

        for meta in results:
            if (
                meta.mime_type == "inode/directory"
                or getattr(meta, "is_dir", False)
                or getattr(meta, "is_mount", False)
            ):
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
            and not getattr(meta, "is_dir", False)
            and not getattr(meta, "is_mount", False)
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

        file_paths = [
            meta.path
            for meta in results
            if meta.mime_type != "inode/directory"
            and not getattr(meta, "is_dir", False)
            and not getattr(meta, "is_mount", False)
        ]
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
        from nexus.core.pagination import PaginatedResult
        from nexus.lib.pagination import encode_cursor

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
        has_more = True

        # Decode encoded cursor to plain path for paginate_iter
        current_cursor_path: str | None = None
        if cursor:
            from nexus.lib.pagination import CursorError, decode_cursor

            try:
                filters = {"prefix": list_prefix, "recursive": recursive, "zone_id": list_zone_id}
                current_cursor_path = decode_cursor(cursor, filters).path
            except CursorError:
                current_cursor_path = None

        while len(collected_items) < limit and has_more:
            from nexus.core.pagination import paginate_iter

            batch = paginate_iter(
                self.metadata.list_iter(prefix=list_prefix, recursive=recursive),
                limit=fetch_limit,
                cursor_path=current_cursor_path,
            )

            from nexus.contracts.constants import SYSTEM_PATH_PREFIX

            batch.items = [
                item
                for item in batch.items
                # Fix nexi-lab/nexus#3733 Bug B: drop synthetic metadata entries
                # (e.g. ns:rebac:*) whose paths are not valid virtual paths.
                if item.path.startswith("/") and not item.path.startswith(SYSTEM_PATH_PREFIX)
            ]

            if self._enforce_permissions and context:
                paths = [item.path for item in batch.items]
                allowed_paths = set(self._permission_enforcer.filter_list(paths, context))
                filtered_items = [item for item in batch.items if item.path in allowed_paths]
            else:
                filtered_items = batch.items

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

    @rpc_expose(description="Find files by glob pattern")
    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,
        files: builtins.list[str] | None = None,
    ) -> builtins.list[str]:
        """Find files matching a glob pattern.

        Supports *, **, ?, [...] patterns. Issue #538: Automatically excludes
        gitignore-style patterns. Results sorted by mtime (newest first).

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "data/*.csv")
            path: Base path to search from (default: "/")
            context: Operation context for permission filtering
            files: #3701 (2A): optional caller-supplied working set. When
                provided, the glob pattern is evaluated against this list
                instead of walking the tree under ``path``. This is the
                stateless narrowing primitive for agent search workflows.
                See ``_validate_and_normalize_files`` for the edge-case
                spec (empty list, traversal, cross-zone, dedupe, stale,
                size cap, permission intersection).
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

        # Phase 2: Get accessible files.
        # Issue #3701 (15A): when the caller passed an explicit files list
        # we skip the full tree walk and use the validated working set as
        # the universe to match against. O(files) instead of O(tree).
        if files is not None:
            validated, _stale = self._validate_and_normalize_files(
                files=files, path=search_path, context=context
            )
            accessible_files: list[str] = validated
            logger.debug(
                f"[GLOB] Phase 2: files=[...] short-circuit "
                f"({len(accessible_files)} files supplied)"
            )
        else:
            list_start = time.time()
            accessible_files = cast(
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
            if self._should_prepend_recursive_wildcard(full_pattern):
                full_pattern = "**/" + full_pattern
        else:
            base_path = path[1:] if path.startswith("/") else path
            # Strip leading "/" from pattern to avoid double-slash when
            # base_path already ends with "/" (e.g., zone-scoped paths).
            pattern_part = pattern.lstrip("/") if pattern.startswith("/") else pattern
            full_pattern = base_path + pattern_part

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
        except Exception as e:
            logger.debug("Failed to list accessible files for glob at %s: %s", path, e)
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
                    if self._should_prepend_recursive_wildcard(full_pattern):
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
    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",  # noqa: ARG002
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        files: builtins.list[str] | None = None,
        block_type: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        r"""Search file contents using regex patterns.

        Uses adaptive algorithm selection (Issue #929) with 5 strategies.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, case-insensitive search
            max_results: Maximum number of results (default: 100)
            search_mode: Unused (reserved for future search mode selection)
            context: Operation context for permission filtering
            before_context: Number of lines to include before each match
            after_context: Number of lines to include after each match
            invert_match: If True, return non-matching lines
            files: #3701 (2A): optional caller-supplied working set of
                file paths. When provided, grep skips the tree walk
                (``self.list``) and searches only the intersection of
                ``files`` with the caller's permitted paths. Composes
                with ``file_pattern``: the final candidate set is
                ``files ∩ glob(file_pattern)`` when both are present.
                See ``_validate_and_normalize_files`` for the full
                edge-case spec.
            block_type: #3720: optional markdown block type filter.
                When set, only return matches from lines inside blocks
                of the given type.  Valid values: ``"code"``,
                ``"table"``, ``"frontmatter"``.  Non-markdown files
                (or markdown files without ``md_structure`` metadata)
                pass through unfiltered.
        """
        if path and path != "/":
            path = self._validate_path(path)

        # Issue #3720: validate block_type early.
        if block_type is not None and block_type not in VALID_BLOCK_TYPES:
            raise ValueError(
                f"Invalid block_type {block_type!r}. "
                f"Valid values: {', '.join(sorted(VALID_BLOCK_TYPES))}"
            )

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Issue #3720: over-fetch when block_type filtering will discard
        # some matches.  The original max_results is restored after
        # post-filtering so callers see the expected result count.
        original_max_results = max_results
        if block_type is not None:
            max_results = min(
                max_results * _BLOCK_TYPE_OVERFETCH_FACTOR,
                max(max_results, _BLOCK_TYPE_OVERFETCH_CAP),
            )

        # Phase 1: Get files to search.
        #
        # Issue #3701 precedence (Issue 7A edge (g)):
        #   - files alone            → validated working set
        #   - file_pattern alone     → glob result (pre-existing behaviour)
        #   - both                   → intersection (files ∩ glob)
        #   - neither                → full tree walk (pre-existing behaviour)
        #
        # The intersection ordering matters: validate files FIRST (so
        # traversal/size/cross-zone errors fire before any expensive
        # glob work), then intersect with the glob result.
        candidate_files: list[str]
        if files is not None:
            validated, _stale = self._validate_and_normalize_files(
                files=files, path=path, context=context
            )
            if file_pattern:
                glob_result = set(self.glob(file_pattern, path, context=context))
                candidate_files = [f for f in validated if f in glob_result]
            else:
                candidate_files = validated
        elif file_pattern:
            candidate_files = self.glob(file_pattern, path, context=context)
        else:
            candidate_files = cast(list[str], self.list(path, recursive=True, context=context))
            pre_filter_count = len(candidate_files)
            candidate_files = _filter_ignored_paths(candidate_files)
            if pre_filter_count != len(candidate_files):
                logger.debug(
                    f"[GREP] Issue #538: Filtered {pre_filter_count - len(candidate_files)} paths"
                )

        if not candidate_files:
            return []

        # #3701 Codex finding: only the Python path through ``_grep_lines``
        # honours ``before_context`` / ``after_context`` / ``invert_match``.
        # The accelerated paths (TRIGRAM_INDEX, ZOEKT_INDEX, PARALLEL_POOL,
        # mmap, rust_bulk) all do raw regex scans and silently drop these
        # flags. When any of them are set we route every file through the
        # CACHED_TEXT/SEQUENTIAL Python loop so the flags actually take
        # effect. ``CACHED_TEXT`` runs opportunistically on whatever the
        # metastore has cached; everything else falls through to
        # ``_grep_raw_content`` with ``force_python_path=True``.
        needs_python_path = before_context > 0 or after_context > 0 or invert_match

        # Phase 2: Bulk fetch searchable text
        searchable_texts = self.metadata.get_searchable_text_bulk(candidate_files)
        cached_text_ratio = len(searchable_texts) / len(candidate_files) if candidate_files else 0.0
        files_needing_raw = [f for f in candidate_files if f not in searchable_texts]

        # Phase 3: Select strategy (Issue #929, #954)
        # Issue #3701 (13A): when the caller supplied a ``files`` list AND
        # the list is small enough to make direct scanning cheaper than
        # trigram + post-filter, we bypass the trigram strategy. The
        # threshold lives in ``FILES_FILTER_TRIGRAM_THRESHOLD`` and is
        # benchmark-backed.
        zone_id, _, _ = self._get_routing_params(context)
        if block_type is not None:
            # Issue #3720 (Codex R2+R5): block_type MUST use SEQUENTIAL
            # to ensure ALL files (cached + uncached) are searched.
            # CACHED_TEXT skips files_needing_raw; TRIGRAM/ZOEKT return
            # a fixed page that may miss qualifying block matches.
            strategy = SearchStrategy.SEQUENTIAL
        elif needs_python_path:
            # Force a Python-loop strategy so context/invert flags take
            # effect. CACHED_TEXT is preferred when the metastore has
            # text cached for most candidates; otherwise SEQUENTIAL
            # routes through ``_grep_raw_content`` which will skip its
            # mmap+rust accelerator branches via ``force_python_path``.
            strategy = (
                SearchStrategy.CACHED_TEXT
                if cached_text_ratio >= GREP_CACHED_TEXT_RATIO
                else SearchStrategy.SEQUENTIAL
            )
        else:
            # Issue #3711: Kick off background trigram index build when
            # the file count exceeds the threshold but no index exists.
            # The current grep proceeds without blocking; the *next*
            # grep will find the index and use the fast path.
            if (
                len(candidate_files) > GREP_TRIGRAM_THRESHOLD
                and zone_id
                and trigram_fast.is_available()
                and not trigram_fast.index_exists(zone_id)
            ):
                task = asyncio.ensure_future(
                    self._build_trigram_background(zone_id, context),
                )
                # Suppress "exception was never retrieved" warning.
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            strategy = self._select_grep_strategy(
                file_count=len(candidate_files),
                cached_text_ratio=cached_text_ratio,
                zone_id=zone_id,
            )
            if (
                files is not None
                and strategy == SearchStrategy.TRIGRAM_INDEX
                and len(candidate_files) < FILES_FILTER_TRIGRAM_THRESHOLD
            ):
                logger.debug(
                    "[GREP] files=[...] of size %d below trigram threshold %d — "
                    "bypassing TRIGRAM_INDEX in favour of direct scan",
                    len(candidate_files),
                    FILES_FILTER_TRIGRAM_THRESHOLD,
                )
                strategy = (
                    SearchStrategy.CACHED_TEXT
                    if cached_text_ratio >= GREP_CACHED_TEXT_RATIO
                    else SearchStrategy.RUST_BULK
                )

        # Remember whether the caller supplied a files filter so the
        # trigram branch can post-filter its results (#3701 13A).
        _files_filter_set: set[str] | None = set(candidate_files) if files is not None else None

        # Phase 4: Execute strategy-specific search
        results: list[dict[str, Any]] = []

        # Strategy: TRIGRAM_INDEX (Issue #954)
        if strategy == SearchStrategy.TRIGRAM_INDEX and zone_id:
            # #3701 (13A): when trigram runs with a files filter, we over-
            # fetch then post-filter its output so the caller's working
            # set is honoured. Over-fetch factor compensates for the
            # trigram hits that fall outside the filter.
            trigram_max = (
                max_results * _REBAC_OVERFETCH_FACTOR_FOR_FILES
                if _files_filter_set is not None
                else max_results
            )
            trigram_results = await self._try_grep_with_trigram(
                pattern=pattern,
                ignore_case=ignore_case,
                max_results=trigram_max,
                zone_id=zone_id,
                context=context,
            )
            if trigram_results is not None:
                if _files_filter_set is not None:
                    trigram_results = [
                        r for r in trigram_results if r.get("file") in _files_filter_set
                    ][:max_results]
                # Issue #3720: apply block_type post-filter before returning.
                if block_type is not None:
                    trigram_results = self._filter_results_by_block_type(
                        trigram_results, block_type
                    )
                    return trigram_results[:original_max_results]
                return trigram_results
            strategy = SearchStrategy.RUST_BULK  # Fallback

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
                # Issue #3720: apply block_type post-filter before returning.
                if block_type is not None:
                    zoekt_results = self._filter_results_by_block_type(zoekt_results, block_type)
                    return zoekt_results[:original_max_results]
                return zoekt_results
            strategy = SearchStrategy.RUST_BULK

        # Strategy: CACHED_TEXT or opportunistic cached text search
        if strategy == SearchStrategy.CACHED_TEXT or searchable_texts:
            for file_path, text in searchable_texts.items():
                if len(results) >= max_results:
                    break
                lines = text.splitlines()
                results.extend(
                    self._grep_lines(
                        regex=regex,
                        lines=lines,
                        file_path=file_path,
                        before_context=before_context,
                        after_context=after_context,
                        invert_match=invert_match,
                        max_results=max_results - len(results),
                    )
                )
            if (
                strategy == SearchStrategy.CACHED_TEXT
                and block_type is None
                and len(results) >= max_results
            ):
                return results[:max_results]

        if block_type is None and len(results) >= max_results:
            return results[:max_results]

        # Process remaining files needing raw content
        if files_needing_raw:
            remaining_results = max_results - len(results)

            if strategy == SearchStrategy.PARALLEL_POOL:
                results.extend(
                    await self._grep_parallel(
                        regex=regex,
                        files=files_needing_raw,
                        max_results=remaining_results,
                        context=context,
                    )
                )
            elif strategy in (SearchStrategy.RUST_BULK, SearchStrategy.SEQUENTIAL):
                results.extend(
                    await self._grep_raw_content(
                        regex=regex,
                        pattern=pattern,
                        files_needing_raw=files_needing_raw,
                        strategy=strategy,
                        ignore_case=ignore_case,
                        remaining_results=remaining_results,
                        context=context,
                        before_context=before_context,
                        after_context=after_context,
                        invert_match=invert_match,
                        force_python_path=needs_python_path,
                    )
                )

        # Issue #3720: post-filter by block_type when requested.
        if block_type is not None:
            results = self._filter_results_by_block_type(results, block_type)
            max_results = original_max_results

        return results[:max_results]

    # =========================================================================
    # Grep Helpers
    # =========================================================================

    @staticmethod
    def _grep_lines(
        regex: re.Pattern[str],
        lines: builtins.list[str],
        file_path: str,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        max_results: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """Search lines with optional context and invert-match support.

        Args:
            regex: Compiled regex pattern
            lines: List of text lines to search
            file_path: File path for result entries
            before_context: Number of context lines before each match
            after_context: Number of context lines after each match
            invert_match: If True, return non-matching lines
            max_results: Maximum results to return
        """
        results: builtins.list[dict[str, Any]] = []

        if invert_match:
            # Return lines that do NOT match
            matching_indices: set[int] = set()
            for idx, line in enumerate(lines):
                if regex.search(line):
                    matching_indices.add(idx)

            for idx, line in enumerate(lines):
                if len(results) >= max_results:
                    break
                if idx not in matching_indices:
                    entry: dict[str, Any] = {
                        "file": file_path,
                        "line": idx + 1,
                        "content": line,
                    }
                    if before_context > 0 or after_context > 0:
                        b_start = max(0, idx - before_context)
                        a_end = min(len(lines), idx + after_context + 1)
                        if before_context > 0:
                            entry["before_context"] = [
                                {"line": i + 1, "content": lines[i]} for i in range(b_start, idx)
                            ]
                        if after_context > 0:
                            entry["after_context"] = [
                                {"line": i + 1, "content": lines[i]} for i in range(idx + 1, a_end)
                            ]
                    results.append(entry)
        else:
            # Normal matching: find matching indices first
            match_data: builtins.list[tuple[int, re.Match[str]]] = []
            for idx, line in enumerate(lines):
                match_obj = regex.search(line)
                if match_obj:
                    match_data.append((idx, match_obj))

            for idx, match_obj in match_data:
                if len(results) >= max_results:
                    break
                entry = {
                    "file": file_path,
                    "line": idx + 1,
                    "content": lines[idx],
                    "match": match_obj.group(0),
                }
                if before_context > 0 or after_context > 0:
                    b_start = max(0, idx - before_context)
                    a_end = min(len(lines), idx + after_context + 1)
                    if before_context > 0:
                        entry["before_context"] = [
                            {"line": i + 1, "content": lines[i]} for i in range(b_start, idx)
                        ]
                    if after_context > 0:
                        entry["after_context"] = [
                            {"line": i + 1, "content": lines[i]} for i in range(idx + 1, a_end)
                        ]
                results.append(entry)

        return results

    def _filter_results_by_block_type(
        self,
        results: builtins.list[dict[str, Any]],
        block_type: str,
    ) -> builtins.list[dict[str, Any]]:
        """Post-filter grep results to lines inside *block_type* regions.

        Issue #3720.  For each ``.md`` file in *results*, fetches the
        ``md_structure`` index from the metastore and keeps only matches
        whose 0-indexed line falls within a block of the requested type.
        Non-markdown files (or markdown files without stored metadata)
        pass through unfiltered.

        Works directly with the raw JSON dict to avoid cross-brick
        imports (``nexus.bricks.parsers`` is a separate brick).
        """
        import json as _json

        MD_STRUCTURE_KEY = "md_structure"  # noqa: N806
        _V2_BLOCK_TYPES = frozenset({"paragraph", "blockquote", "list", "heading"})

        # Group results by file so we fetch metadata once per file.
        by_file: dict[str, builtins.list[dict[str, Any]]] = {}
        for r in results:
            by_file.setdefault(r.get("file", ""), []).append(r)

        filtered: builtins.list[dict[str, Any]] = []
        start = time.monotonic()

        for file_path, file_results in by_file.items():
            if not file_path.lower().endswith(_MARKDOWN_EXTENSIONS):
                # Non-markdown — include all results (decision #4A).
                filtered.extend(file_results)
                continue

            # Fetch md_structure metadata for this file.
            raw = self.metadata.get_file_metadata(file_path, MD_STRUCTURE_KEY)
            if raw is None:
                # Issue #3720 (Codex R6): recognized markdown without
                # metadata → fail closed.
                logger.debug(
                    "No md_structure for %s — excluding results (fail closed)",
                    file_path,
                )
                continue

            try:
                data: dict[str, Any] = raw if isinstance(raw, dict) else _json.loads(raw)
            except Exception:
                # Issue #3720 (Codex R7): fail closed on corrupt metadata.
                logger.debug("Corrupt md_structure for %s — excluding results", file_path)
                continue

            # Issue #3720 (Codex R1+R2): v1 indices don't contain the
            # new block types. Fail closed.
            version = data.get("version", 1)
            if version < 2 and block_type in _V2_BLOCK_TYPES:
                logger.warning(
                    "md_structure v%d for %s lacks %s blocks — "
                    "excluding results until file is reindexed "
                    "(rewrite the file or run a reindex to upgrade)",
                    version,
                    file_path,
                    block_type,
                )
                continue

            # Build a flat list of (line_start, line_end) for the requested
            # block type. Normalize frontmatter into the same shape.
            block_ranges: builtins.list[tuple[int, int]] = []
            if block_type == "frontmatter":
                fm = data.get("frontmatter")
                if fm is not None:
                    block_ranges.append((fm["line_start"], fm["line_end"]))
            else:
                for section in data.get("sections", []):
                    for blk in section.get("blocks", []):
                        if blk.get("type") == block_type:
                            block_ranges.append((blk["line_start"], blk["line_end"]))

            # Filter: keep results whose 0-indexed line falls in a range.
            for r in file_results:
                line_0 = r.get("line", 0) - 1  # grep results are 1-indexed
                for rng_start, rng_end in block_ranges:
                    if rng_start <= line_0 < rng_end:
                        filtered.append(r)
                        break

        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > 5:
            logger.debug(
                "[GREP] Issue #3720: block_type=%s filter took %.1f ms (%d→%d results, %d files)",
                block_type,
                elapsed_ms,
                len(results),
                len(filtered),
                len(by_file),
            )

        return filtered

    async def _grep_raw_content(
        self,
        regex: re.Pattern[str],
        pattern: str,
        files_needing_raw: builtins.list[str],
        strategy: SearchStrategy,
        ignore_case: bool,
        remaining_results: int,
        context: Any,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        force_python_path: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """Process files needing raw content read (mmap, Rust bulk, sequential).

        When ``force_python_path`` is True the mmap and Rust bulk
        accelerators are skipped and every file is run through the
        Python sequential fallback. This exists because those
        accelerators do raw regex scans without honouring
        ``before_context`` / ``after_context`` / ``invert_match`` —
        flags that ``_grep_lines`` (called from the sequential
        fallback) does honour.
        """
        results: builtins.list[dict[str, Any]] = []
        mmap_used = False

        # Try mmap-accelerated grep first (Issue #893). Skipped when the
        # caller asked for context/invert flags — mmap doesn't honour
        # them and would silently drop them.
        if not force_python_path and grep_fast.is_mmap_available():
            try:
                zone_id, _, _ = self._extract_zone_info(context)
                if zone_id and self._file_cache is not None:
                    file_cache = self._file_cache
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
                    read_result = await self._read(file_path, context=context)
                    if not isinstance(read_result, bytes):
                        continue
                    try:
                        text = read_result.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    lines = text.splitlines()
                    results.extend(
                        self._grep_lines(
                            regex=regex,
                            lines=lines,
                            file_path=file_path,
                            before_context=before_context,
                            after_context=after_context,
                            invert_match=invert_match,
                            max_results=remaining_results - len(results),
                        )
                    )
                except Exception as e:
                    logger.debug("Failed to grep file %s: %s", file_path, e)
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
        if self._zoekt_client is None:
            return None

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return None
            is_available = loop.run_until_complete(self._zoekt_client.is_available())
        except RuntimeError:
            is_available = asyncio.run(self._zoekt_client.is_available())

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
                matches = loop.run_until_complete(
                    self._zoekt_client.search(zoekt_query, num=max_results * 3)
                )
            except RuntimeError:
                matches = asyncio.run(self._zoekt_client.search(zoekt_query, num=max_results * 3))

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

    async def _try_grep_with_trigram(
        self,
        pattern: str,
        ignore_case: bool,
        max_results: int,
        zone_id: str,
        context: Any = None,
    ) -> builtins.list[dict[str, Any]] | None:
        """Try trigram index for accelerated grep (Issue #954).

        Uses trigram index for O(1) candidate lookup, then verifies candidates
        by reading content through NexusFS (supporting CAS backends).

        Returns None if trigram index is not available or on error,
        allowing fallback to other strategies.
        """
        if not trigram_fast.is_available():
            return None

        index_path = trigram_fast.get_index_path(zone_id)
        if not os.path.isfile(index_path):
            return None

        # Phase 1: Get candidate file paths from trigram index (sub-ms).
        candidates = trigram_fast.search_candidates(
            index_path=index_path,
            pattern=pattern,
            ignore_case=ignore_case,
        )

        if candidates is None:
            logger.warning("[GREP] Trigram candidate search failed, falling back")
            return None

        if not candidates:
            logger.debug("[GREP] Issue #954: Trigram index found 0 candidates for zone=%s", zone_id)
            return []

        logger.debug(
            "[GREP] Issue #954: Trigram index found %d candidates for zone=%s",
            len(candidates),
            zone_id,
        )

        # Phase 2: Verify candidates by reading content through NexusFS.
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            return None

        results: builtins.list[dict[str, Any]] = []
        for file_path in candidates:
            if len(results) >= max_results:
                break
            try:
                content = await self._read(file_path, context=context)
                if not isinstance(content, bytes):
                    continue
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
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
            except Exception as e:
                logger.debug("Failed to read/search trigram candidate %s: %s", file_path, e)
                continue

        return results

    # Dedup guard for background trigram builds.  Safe without a lock
    # because grep() runs in a single asyncio event loop and there are
    # no await points between the check and the add.
    _trigram_build_in_progress: set[str] = set()

    async def _build_trigram_background(self, zone_id: str, context: Any = None) -> None:
        """Issue #3711: Non-blocking trigram index build.

        Fires and forgets — errors are logged, not raised.  A guard set
        prevents duplicate builds for the same zone.
        """
        if zone_id in self._trigram_build_in_progress:
            return
        self._trigram_build_in_progress.add(zone_id)
        try:
            await self.build_trigram_index_for_zone(zone_id, context=context)
        except Exception:
            logger.debug(
                "[GREP] Issue #3711: Background trigram build failed for zone=%s",
                zone_id,
                exc_info=True,
            )
        finally:
            self._trigram_build_in_progress.discard(zone_id)

    async def build_trigram_index_for_zone(
        self,
        zone_id: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Build trigram index for all files in a zone (Issue #954).

        Reads file content through NexusFS (supporting CAS backends) and
        builds the index using (virtual_path, content) pairs.

        Args:
            zone_id: Zone identifier.
            context: Operation context for permission filtering.

        Returns:
            Dict with status, file_count, trigram_count, index_size_bytes.
        """
        if not trigram_fast.is_available():
            return {"status": "unavailable", "reason": "Rust extension not available"}

        # List all files in the zone.
        files = cast(list[str], self.list("/", recursive=True, context=context))

        index_path = trigram_fast.get_index_path(zone_id)
        os.makedirs(os.path.dirname(index_path), exist_ok=True)

        # Read content through NexusFS and build index from (path, content) pairs.
        # This works with any backend (CAS, S3, etc.) since we read through the
        # NexusFS abstraction rather than directly from disk.
        entries: builtins.list[tuple[str, bytes]] = []
        for file_path in files:
            try:
                content = await self._read(file_path, context=context)
                if isinstance(content, bytes):
                    entries.append((file_path, content))
            except Exception as e:
                logger.debug(
                    "Skipping unreadable file during trigram indexing %s: %s", file_path, e
                )
                continue

        success = trigram_fast.build_index_from_entries(entries, index_path)
        if not success:
            return {"status": "error", "reason": "Index build failed"}

        stats = trigram_fast.get_stats(index_path)
        return {
            "status": "ok",
            "index_path": index_path,
            **(stats or {}),
        }

    def get_trigram_index_status(self, zone_id: str) -> dict[str, Any]:
        """Get trigram index status for a zone (Issue #954)."""
        if not trigram_fast.is_available():
            return {"status": "unavailable"}

        index_path = trigram_fast.get_index_path(zone_id)
        if not os.path.isfile(index_path):
            return {"status": "not_built", "index_path": index_path}

        stats = trigram_fast.get_stats(index_path)
        if stats is None:
            return {"status": "error", "index_path": index_path}

        return {"status": "ok", "index_path": index_path, **stats}

    def invalidate_trigram_index(self, zone_id: str) -> None:
        """Delete trigram index for a zone and clear cache (Issue #954)."""
        index_path = trigram_fast.get_index_path(zone_id)
        trigram_fast.invalidate_cache(index_path)
        if os.path.isfile(index_path):
            os.remove(index_path)

    async def _grep_parallel(
        self,
        regex: re.Pattern[str],
        files: builtins.list[str],
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Parallel grep using asyncio.gather (Issue #929).

        Each worker searches its chunk independently. Results are merged and
        truncated to ``max_results`` in the caller.
        """
        from nexus.utils.timing import Timer

        timer = Timer()
        timer.__enter__()

        chunk_size = max(1, len(files) // self._grep_parallel_workers)
        file_chunks = [files[i : i + chunk_size] for i in range(0, len(files), chunk_size)]

        async def search_chunk(
            chunk_files: builtins.list[str],
        ) -> builtins.list[dict[str, Any]]:
            chunk_results: builtins.list[dict[str, Any]] = []
            for file_path in chunk_files:
                try:
                    read_result = await self._read(file_path, context=context)
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
                except Exception as e:
                    logger.debug("Failed to grep file in parallel chunk %s: %s", file_path, e)
                    continue
            return chunk_results

        all_results: builtins.list[dict[str, Any]] = []
        chunk_coros = [search_chunk(chunk) for chunk in file_chunks]
        chunk_results_list = await asyncio.gather(*chunk_coros, return_exceptions=True)
        for chunk_result in chunk_results_list:
            if isinstance(chunk_result, BaseException):
                logger.debug(f"[GREP-PARALLEL] Chunk failed: {chunk_result}")
                continue
            all_results.extend(chunk_result)
            if len(all_results) >= max_results:
                break

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
        if self._zoekt_client is None:
            return False

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return False
            return loop.run_until_complete(self._zoekt_client.is_available())
        except RuntimeError:
            return asyncio.run(self._zoekt_client.is_available())
        except Exception:
            return False

    def _select_grep_strategy(
        self,
        file_count: int,
        cached_text_ratio: float,
        zoekt_available: bool | None = None,
        zone_id: str | None = None,
    ) -> SearchStrategy:
        """Select optimal grep strategy (Issue #929, #954, #3711)."""
        if cached_text_ratio >= GREP_CACHED_TEXT_RATIO:
            return SearchStrategy.CACHED_TEXT
        if file_count < GREP_SEQUENTIAL_THRESHOLD:
            return SearchStrategy.SEQUENTIAL
        # Issue #954: Trigram index — prefer for large file sets with built index.
        if (
            file_count > GREP_TRIGRAM_THRESHOLD
            and zone_id
            and trigram_fast.is_available()
            and trigram_fast.index_exists(zone_id)
        ):
            return SearchStrategy.TRIGRAM_INDEX
        if file_count > GREP_ZOEKT_THRESHOLD:
            if zoekt_available is None:
                zoekt_available = self._is_zoekt_available()
            if zoekt_available:
                return SearchStrategy.ZOEKT_INDEX
        # Issue #3711: Rust bulk (400x) >> Python parallel pool (4x).
        # PARALLEL_POOL is only useful when Rust is unavailable.
        if grep_fast.is_available():
            return SearchStrategy.RUST_BULK
        if GREP_PARALLEL_THRESHOLD <= file_count <= 10000:
            return SearchStrategy.PARALLEL_POOL
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

    def _validate_and_normalize_files(
        self,
        files: builtins.list[str],
        path: str,  # noqa: ARG002 — kept for API compat; no longer used after Codex review #3 fix
        context: Any,
    ) -> tuple[builtins.list[str], int]:
        """Validate and normalize a caller-supplied files list for grep/glob.

        Implements the 9-row edge-case matrix from the #3701 review
        (Issue 7A). Returns ``(validated_files, stale_count)`` where
        ``validated_files`` is deduped, normalised, and intersected with
        the files the caller is permitted to see under ``path`` +
        ``context``. ``stale_count`` counts entries in ``files`` that
        were dropped because they were not found in the permitted set
        (deleted, permission-denied, or otherwise invisible).

        Edge cases:
            (a) ``files == []`` → returns ``([], 0)`` without calling
                ``self.list`` — explicit no-op, fast.
            (b) Path traversal (``..``) → ``InvalidPathError`` via
                ``_validate_path``.
            (c) Cross-zone entries → ``ValueError``. Callers that need
                cross-zone searching must make separate per-zone calls.
            (d) Duplicates → silently deduped (set semantics).
            (e) Stale/deleted → silently skipped, counted in
                ``stale_count`` so the response envelope can surface drift.
            (f) ``len(files) > FILES_FILTER_SIZE_CAP`` → ``ValueError``
                before any other work (fail fast on abuse).
            (g) Interaction with ``file_pattern`` → caller performs the
                intersection; this helper only deals with ``files``.
            (h) Path normalisation → leading slash added, trailing
                slashes preserved as-is, no case folding.
            (i) Permissions → authorised directly via the injected
                ``PermissionEnforcer.filter_list`` (Codex review #3
                finding #2: no recursive tree walk — O(files) instead
                of O(tree)).

        Args:
            files: Caller-supplied list of file paths.
            path: Base path the operation is scoped to — used for the
                permission intersection. If the caller supplies files
                outside this subtree they are treated as stale (dropped).
            context: Operation context for permission resolution.

        Returns:
            Tuple of ``(validated_files, stale_count)``.

        Raises:
            InvalidPathError: If any entry has a traversal segment.
            ValueError: If the list is too large, or an entry refers to
                a different zone than the current caller's context.
        """
        # (f) Size cap — fail fast before spending any time on other work.
        if len(files) > FILES_FILTER_SIZE_CAP:
            raise ValueError(
                f"files list too large: {len(files)} > {FILES_FILTER_SIZE_CAP} (cap to avoid abuse)"
            )

        # (a) Empty list — explicit no-op, no tree walk.
        if not files:
            return [], 0

        # Extract the caller's zone so we can reject cross-zone entries
        # early. When context is None we cannot enforce zone scoping at
        # this layer — fall back to permission-intersection only.
        caller_zone: str | None = None
        try:
            caller_zone, _, _ = self._get_routing_params(context)
        except Exception:
            caller_zone = None

        normalised: list[str] = []
        seen: set[str] = set()
        for raw in files:
            # (b) Path traversal — delegates to the shared validator.
            validated = self._validate_path(raw)

            # (h) Normalise: ensure leading slash; keep the rest of the
            # path byte-identical. Case is preserved (case-sensitive
            # filesystems are the common case in nexus zones).
            if not validated.startswith("/"):
                validated = "/" + validated

            # (c) Cross-zone rejection. We recognise zone-aware prefixes
            # and compare against the caller's zone. Non-zone-prefixed
            # paths are always allowed through; permission intersection
            # below is the remaining defence.
            if caller_zone is not None and validated.startswith("/zones/"):
                parts = validated.split("/", 3)
                if len(parts) >= 3 and parts[2] and parts[2] != caller_zone:
                    raise ValueError(
                        f"cross-zone file rejected: {validated!r} is in "
                        f"zone {parts[2]!r} but caller is in {caller_zone!r}"
                    )

            # (d) Dedupe while preserving first-seen order.
            if validated not in seen:
                seen.add(validated)
                normalised.append(validated)

        # (i) Permission intersection: only keep files the caller is
        # actually allowed to see. We must avoid the recursive tree
        # walk here — Codex review #3 (finding #2) flagged that the
        # previous implementation called
        # ``self.list(path, recursive=True, context=context)`` which
        # defeats the whole ``O(files)`` promise of ``files=[...]``
        # (large repos hit the metastore for the entire subtree and
        # time out under load). Instead we authorise the caller's list
        # directly via ``filter_list``, which is implemented on the
        # ReBAC enforcer as a bulk strategy chain — ``O(files)``, not
        # ``O(tree)``.
        #
        # This also sidesteps Codex review #2 finding #1 (zone scoping
        # mismatch) because we never call ``self.list`` here — there's
        # no list output to unscope, and the enforcer's ``filter_list``
        # accepts the caller's paths in the same namespace the caller
        # supplied them (no internal zone prefix).
        resolved_context = context if context is not None else self._default_context
        if self._enforce_permissions and self._permission_enforcer is not None:
            permitted_list = self._permission_enforcer.filter_list(normalised, resolved_context)
            permitted_set = set(permitted_list)
        else:
            # Permissions disabled — no filtering, everything passes.
            permitted_set = set(normalised)

        # (e) Stale silent skip — drop entries not in permitted_set
        # (either unreadable to the caller or filtered by the enforcer)
        # and report the count so the caller can detect drift.
        # Note: we intentionally do NOT do a separate existence check
        # here; if the caller names a file that doesn't exist, the
        # downstream grep/glob will simply produce zero matches for it,
        # which is the same observable end result.
        visible = [p for p in normalised if p in permitted_set]
        stale_count = len(normalised) - len(visible)
        return visible, stale_count

    # =========================================================================
    # Semantic Search (inlined from SemanticSearchMixin, Issue #1287, #2075)
    # =========================================================================

    @property
    def _has_search_engine(self) -> bool:
        """Check if a search engine is available.

        Since Issue #2663 (txtai migration), ``_query_service`` is always
        ``None``; indexing uses ``_pipeline_indexer`` / ``_indexing_service``.
        """
        return (
            self._query_service is not None
            or self._pipeline_indexer is not None
            or self._indexing_service is not None
        )

    def _require_search_engine(self) -> None:
        """Raise ValueError if no search engine is initialized."""
        if not self._has_search_engine:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await search.initialize_semantic_search()"
            )

    async def ainitialize_semantic_search(
        self,
        *,
        nx: Any,
        record_store_engine: Any,  # noqa: ARG002
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        chunk_size: int = 1024,
        chunk_strategy: str = "semantic",
        async_mode: bool = True,  # noqa: ARG002
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine (NexusFS path).

        Delegates to factory helper for component creation (Issue #2075, DRY).
        """
        from nexus.factory._semantic_search import create_semantic_search_components

        if self._record_store is None:
            raise RuntimeError("Semantic search requires RecordStore (SQL engine)")

        components = await create_semantic_search_components(
            record_store=self._record_store,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            api_key=api_key,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            cache_url=cache_url,
            embedding_cache_ttl=embedding_cache_ttl,
            nx=nx,
        )
        self._query_service = components.query_service
        self._indexing_service = components.indexing_service
        self._indexing_pipeline = components.indexing_pipeline
        self._pipeline_indexer = components.pipeline_indexer

    async def _semantic_with_sandbox_fallback(
        self,
        federation_call: "Any",
        bm25s_call: "Any",
    ) -> "builtins.list[Any]":
        """Run federated semantic search with SANDBOX-profile BM25S fallback.

        Issue #3778. When the active profile is SANDBOX and federation reports
        that every peer failed (see ``is_all_peers_failed``), we fall back to
        the local BM25S callable and stamp each result with
        ``semantic_degraded=True``. A WARNING is logged only on the first
        fallback per ``SearchService`` instance; subsequent fallbacks are
        silent to avoid flooding a long-running sandbox's logs.

        The callables are supplied by the caller so this method is easy to
        test and has no hard dependency on specific federation / BM25S
        constructor shapes.

        Args:
            federation_call: zero-arg awaitable that returns a
                ``FederatedSearchResponse``. Wrap the real dispatcher's
                ``.search(...)`` with functools.partial or a lambda.
            bm25s_call: zero-arg awaitable that returns a list of
                ``BaseSearchResult`` (or any object with ``semantic_degraded``
                assignable). Executed only when federation reports all peers
                failed AND the profile is SANDBOX.

        Returns:
            A list of results. When the SANDBOX fallback kicks in, each item
            has ``semantic_degraded = True``. Otherwise the federation's
            results are returned as-is (``semantic_degraded`` unset).
        """
        # Defer imports so the main code path doesn't pay for them.
        from nexus.bricks.search.federated_search import is_all_peers_failed

        fed_response = await federation_call()

        is_sandbox = self._deployment_profile == "sandbox"
        if not is_sandbox:
            return list(fed_response.results)

        if not is_all_peers_failed(fed_response):
            return list(fed_response.results)

        # Record degradation in the contextvar so envelope builders (MCP/HTTP)
        # can detect it even if the BM25S fallback returned zero items.
        LAST_SEMANTIC_DEGRADED.set(True)

        # SANDBOX + all peers failed → fall back to local BM25S.
        if not self._sandbox_fallback_warned:
            logger.warning(
                "[SearchService] SANDBOX: federation unreachable (zones_searched=%d, "
                "zones_failed=%d) — degrading semantic search to local BM25S. "
                "Results will be marked semantic_degraded=True. Further occurrences "
                "will be logged at DEBUG.",
                len(fed_response.zones_searched),
                len(fed_response.zones_failed),
            )
            self._sandbox_fallback_warned = True
        else:
            logger.debug("[SearchService] SANDBOX semantic fallback to BM25S (already warned once)")

        import contextlib

        bm25s_results = await bm25s_call()
        stamped: builtins.list[Any] = []
        for r in bm25s_results:
            # Result may not accept the attribute (e.g. a plain dict / frozen
            # dataclass) — in that case skip stamping but still return it so
            # the caller gets *something*.
            with contextlib.suppress(AttributeError):
                r.semantic_degraded = True
            stamped.append(r)
        return stamped

    async def _try_sqlite_vec_sandbox(
        self,
        *,
        query: str,
        path: str,
        limit: int,
        context: "OperationContext | None",
    ) -> builtins.list[dict[str, Any]] | None:
        """Issue #3778: try the local sqlite-vec backend first on SANDBOX.

        Returns:
            * a non-empty list of dict results when the backend is wired and
              KNN returned hits — caller should NOT stamp ``semantic_degraded``
              because this is a *real* semantic match.
            * ``None`` when the backend is absent, errored, or returned no
              hits — caller falls through to the federation/BM25S chain.
        """
        backend = self._sqlite_vec_backend
        if backend is None:
            return None

        zone_id = getattr(context, "zone_id", None) if context else None
        if not zone_id:
            zone_id = ROOT_ZONE_ID

        try:
            from nexus.server.path_utils import unscope_internal_path as _unscope

            db_path = _unscope(path) if path != "/" else None
            fetch_limit = limit * 3 if self._permission_enforcer else limit
            results = await backend.search(
                query=query,
                limit=fetch_limit,
                zone_id=zone_id,
                search_type="hybrid",
                path_filter=db_path,
            )
        except Exception as exc:
            logger.warning(
                "[SearchService] SANDBOX local sqlite-vec search failed (%s); "
                "falling back to federation/BM25S chain",
                exc,
            )
            return None

        if not results:
            return None

        hits: builtins.list[dict[str, Any]] = []
        for r in results:
            entry: dict[str, Any] = {
                "path": r.path,
                "chunk_text": getattr(r, "chunk_text", ""),
                "score": round(r.score, 4),
                "chunk_index": getattr(r, "chunk_index", 0),
                "start_offset": getattr(r, "start_offset", 0) or 0,
                "end_offset": getattr(r, "end_offset", 0) or 0,
                "line_start": getattr(r, "line_start", 0) or 0,
                "line_end": getattr(r, "line_end", 0) or 0,
            }
            ctx_val = getattr(r, "context", None)
            if ctx_val is not None:
                entry["context"] = ctx_val
            hits.append(entry)

        if self._permission_enforcer and hits and context is not None:
            all_paths = [h["path"] for h in hits]
            accessible = set(self._permission_enforcer.filter_list(all_paths, context))
            hits = [h for h in hits if h["path"] in accessible]

        return hits[:limit] if hits else None

    async def _semantic_search_sandbox(
        self,
        *,
        query: str,
        path: str,
        limit: int,
        context: "OperationContext | None",
    ) -> builtins.list[dict[str, Any]]:
        """SANDBOX-profile semantic_search: local vec → federation → BM25S.

        Issue #3778. The fallback chain on SANDBOX is:

        1. **Local sqlite-vec** (``self._sqlite_vec_backend``). When wired
           and the KNN query returns hits, those hits are returned directly
           and ``semantic_degraded`` is NOT set (this is a real semantic
           match, just on a local store rather than a federated peer).
        2. **Federation**: SANDBOX never has peers configured, so the
           ``FederatedSearchResponse`` is synthesised as "no peers" — that
           causes ``_semantic_with_sandbox_fallback`` to invoke the BM25S
           callable.
        3. **BM25S** (via the local SearchDaemon's keyword path), or the
           SQL chunk search when no daemon is wired. Results carry
           ``semantic_degraded=True`` so MCP / HTTP clients can warn users
           that the answer is keyword-only.
        """
        # Reset the degraded flag at the entry point of a SANDBOX search so
        # callers read a value that reflects THIS call only. The contextvar
        # is then set to True inside _semantic_with_sandbox_fallback when
        # fallback actually fires.
        LAST_SEMANTIC_DEGRADED.set(False)

        # Step 1 — try the local vector backend first.
        local = await self._try_sqlite_vec_sandbox(
            query=query, path=path, limit=limit, context=context
        )
        if local is not None:
            return local

        # Step 2 + 3 — try a real federation dispatcher when one is wired;
        # otherwise synthesise an empty FederatedSearchResponse so the
        # shared fallback wrapper invokes the BM25S callable and stamps
        # ``semantic_degraded=True`` on every result.
        from nexus.bricks.search.federated_search import (
            FederatedSearchResponse,
            ZoneFailure,
        )

        async def _fed_call() -> FederatedSearchResponse:
            dispatcher = self._federation_dispatcher
            if dispatcher is not None:
                # R1 review: real dispatcher present — invoke it so we don't
                # silently bypass remote peers and return keyword fallback
                # when semantic retrieval is actually reachable. The wrapper
                # only degrades when all peers fail.
                try:
                    subject = (
                        (getattr(context, "subject_type", None) or "user"),
                        (getattr(context, "user_id", None) or ""),
                    )
                    return await dispatcher.search(
                        query=query,
                        subject=subject,
                        search_type="semantic",
                        limit=limit,
                    )
                except Exception as exc:
                    # Real dispatch failed — treat as all-peers-failed so the
                    # wrapper triggers BM25 fallback with semantic_degraded.
                    logger.warning(
                        "[SANDBOX semantic] federation dispatch raised; degrading to BM25S: %s",
                        exc,
                    )
                    return FederatedSearchResponse(
                        results=[],
                        zones_searched=[],
                        zones_failed=[ZoneFailure(zone_id="<dispatcher>", error=str(exc))],
                    )

            # No dispatcher wired (true SANDBOX case) — is_all_peers_failed
            # returns True and the wrapper invokes the BM25S callable below.
            return FederatedSearchResponse(
                results=[],
                zones_searched=[],
                zones_failed=[],
            )

        async def _bm25s_call() -> builtins.list[dict[str, Any]]:
            # Prefer the daemon's keyword path (BM25S when available).
            daemon = getattr(self, "_search_daemon", None)
            if daemon is not None and getattr(daemon, "_backend", None) is not None:
                fetch_limit = limit * 3 if self._permission_enforcer else limit
                zone_id = getattr(context, "zone_id", None) if context else None
                from nexus.server.path_utils import unscope_internal_path as _unscope

                db_path = _unscope(path) if path != "/" else None
                daemon_results = await daemon.search(
                    query=query,
                    search_type="keyword",
                    limit=fetch_limit,
                    path_filter=db_path,
                    zone_id=zone_id,
                )
                hits: builtins.list[dict[str, Any]] = []
                for r in daemon_results:
                    entry: dict[str, Any] = {
                        "path": r.path,
                        "chunk_text": getattr(r, "chunk_text", ""),
                        "score": round(r.score, 4),
                        "chunk_index": getattr(r, "chunk_index", 0),
                        "start_offset": getattr(r, "start_offset", 0) or 0,
                        "end_offset": getattr(r, "end_offset", 0) or 0,
                        "line_start": getattr(r, "line_start", 0) or 0,
                        "line_end": getattr(r, "line_end", 0) or 0,
                    }
                    ctx_val = getattr(r, "context", None)
                    if ctx_val is not None:
                        entry["context"] = ctx_val
                    hits.append(entry)

                if self._permission_enforcer and hits and context is not None:
                    all_paths = [h["path"] for h in hits]
                    accessible = set(self._permission_enforcer.filter_list(all_paths, context))
                    hits = [h for h in hits if h["path"] in accessible]

                return hits[:limit]

            # No daemon wired — fall back to the SQL chunk search so SANDBOX
            # still returns *something* when a RecordStore is present.
            if self._record_store is not None:
                return await self._sql_chunk_search(query, path, limit, context=context)

            return []

        stamped = await self._semantic_with_sandbox_fallback(_fed_call, _bm25s_call)
        # Only mark items degraded when the fallback actually ran. If a real
        # federation dispatcher returned reachable-peer results, the helper
        # returns them directly without setting LAST_SEMANTIC_DEGRADED, and
        # stamping them here would trigger false "degraded" warnings in
        # downstream envelopes (R3 review).
        degraded = LAST_SEMANTIC_DEGRADED.get()
        out: builtins.list[dict[str, Any]] = []
        for r in stamped:
            if isinstance(r, dict):
                if degraded:
                    r["semantic_degraded"] = True
                out.append(r)
            else:
                # _bm25s_call emits dicts; non-dict can come from a real
                # federation result (BaseSearchResult). Preserve path and
                # only stamp when degraded.
                entry: dict[str, Any] = {"path": getattr(r, "path", "")}
                if degraded:
                    entry["semantic_degraded"] = True
                out.append(entry)
        return out

    @rpc_expose(description="Search documents using natural language queries")
    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,  # noqa: ARG002
        search_mode: str = "semantic",
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search documents using natural language queries.

        Args:
            query: Natural language query
            path: Root path to search
            limit: Maximum number of results
            filters: Optional filters (currently unused)
            search_mode: "keyword", "semantic", or "hybrid"

        Raises:
            ValueError: If semantic search is not initialized
        """
        # Issue #3778: SANDBOX profile has no federated peers — any semantic
        # request must degrade to local BM25S (via daemon keyword search)
        # and stamp ``semantic_degraded=True`` on every result.  We delegate
        # the "no-peers" detection + stamping to _semantic_with_sandbox_fallback
        # so the fallback logic is shared with any future federation caller.
        if self._deployment_profile == "sandbox" and search_mode in ("semantic", "hybrid"):
            return await self._semantic_search_sandbox(
                query=query,
                path=path,
                limit=limit,
                context=context,
            )

        # Issue #2663: _query_service was removed (txtai handles search via
        # SearchDaemon).  When available, delegate to it; otherwise fall back
        # to a simple SQL ILIKE search on document_chunks.
        if self._query_service is not None:
            results = await self._query_service.search(
                query=query,
                path=path,
                limit=limit,
                search_mode=search_mode,
            )
            return [_result_to_dict(r) for r in results]

        # Delegate to SearchDaemon's txtai backend when wired (Issue #2965)
        daemon = getattr(self, "_search_daemon", None)
        if daemon is not None and getattr(daemon, "_backend", None) is not None:
            # Over-fetch to compensate for permission filtering
            fetch_limit = limit * 3 if self._permission_enforcer else limit
            zone_id = getattr(context, "zone_id", None) if context else None
            # RPC may scope paths as /zone/{id}/...; daemon stores unscoped.
            from nexus.server.path_utils import unscope_internal_path as _unscope

            db_path = _unscope(path) if path != "/" else None
            daemon_results = await daemon.search(
                query=query,
                search_type=search_mode,
                limit=fetch_limit,
                path_filter=db_path,
                zone_id=zone_id,
            )
            hits = []
            for r in daemon_results:
                entry: dict[str, Any] = {
                    "path": r.path,
                    "chunk_text": getattr(r, "chunk_text", ""),
                    "score": round(r.score, 4),
                    "chunk_index": getattr(r, "chunk_index", 0),
                    "start_offset": getattr(r, "start_offset", 0) or 0,
                    "end_offset": getattr(r, "end_offset", 0) or 0,
                    "line_start": getattr(r, "line_start", 0) or 0,
                    "line_end": getattr(r, "line_end", 0) or 0,
                }
                # Issue #3773 (Round-6 review): surface admin-configured path
                # context when the daemon attached one. Omit the key when
                # unset to match the HTTP router's shape contract.
                ctx = getattr(r, "context", None)
                if ctx is not None:
                    entry["context"] = ctx
                hits.append(entry)

            # Filter by read permission — only return files the caller can access
            if self._permission_enforcer and hits and context is not None:
                all_paths = [h["path"] for h in hits]
                accessible = set(self._permission_enforcer.filter_list(all_paths, context))
                hits = [h for h in hits if h["path"] in accessible]

            return hits[:limit]

        if self._record_store is not None:
            return await self._sql_chunk_search(query, path, limit, context=context)

        raise ValueError(
            "Semantic search is not available. No query service or record store configured."
        )

    async def _sql_chunk_search(
        self,
        query: str,
        path: str,
        limit: int,
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """Fallback search via SQL LIKE on document_chunks (Issue #2663).

        Used when _query_service is None (txtai migration removed it).

        The *path* may arrive zone-scoped (``/zone/<id>/…``) from the gRPC
        dispatcher.  We strip the zone prefix and use the inner path for the
        LIKE filter so it matches stored ``virtual_path`` values.

        R5 review (Issue #3778): applies ReBAC permission filtering on the
        returned rows when ``context`` is provided AND an enforcer is wired.
        When permissions are enforced but context is missing, returns no
        results — fail closed so the SANDBOX degraded path can't leak
        chunks the caller shouldn't see.
        """
        if self._record_store is None:
            return []

        # Strip zone prefix injected by _scope_params_for_zone.
        # E.g. "/zone/default/" → inner_path="/", "/zone/default/docs" → "/docs"
        import re

        from sqlalchemy import text as sa_text

        zone_match = re.match(r"^/zone/[^/]+(/.*)?$", path)
        if zone_match:
            path = zone_match.group(1) or "/"

        # Filter stopwords and short tokens for better recall
        _STOPWORDS = frozenset(
            {
                "a",
                "an",
                "the",
                "is",
                "it",
                "in",
                "on",
                "at",
                "to",
                "of",
                "and",
                "or",
                "for",
                "by",
                "how",
                "does",
                "do",
                "what",
                "why",
                "this",
                "that",
                "with",
                "from",
            }
        )
        keywords = [
            w.strip().lower()
            for w in query.split()
            if len(w.strip()) >= 2 and w.strip().lower() not in _STOPWORDS
        ]
        if not keywords:
            return []

        # Build WHERE clause: chunk_text ILIKE any keyword (OR for recall)
        conditions = []
        bind_params: dict[str, Any] = {
            "path_prefix": f"{path}%",
            "lim": limit,
        }
        for i, kw in enumerate(keywords[:5]):  # max 5 keywords
            key = f"kw{i}"
            conditions.append(f"dc.chunk_text ILIKE :{key}")
            bind_params[key] = f"%{kw}%"

        where_clause = " OR ".join(conditions)
        sql = sa_text(f"""
            SELECT dc.chunk_text, dc.chunk_index, dc.start_offset,
                   dc.end_offset, dc.line_start, dc.line_end,
                   fp.virtual_path
            FROM document_chunks dc
            JOIN file_paths fp ON dc.path_id = fp.path_id
            WHERE fp.virtual_path LIKE :path_prefix
              AND {where_clause}
            LIMIT :lim
        """)

        def _run_query() -> list:
            session = self._record_store.session_factory()
            try:
                result = session.execute(sql, bind_params)
                return result.fetchall()
            finally:
                session.close()

        try:
            rows = await asyncio.to_thread(_run_query)
        except Exception as e:
            logger.warning("SQL chunk search failed: %s", e, exc_info=True)
            return []

        hits = []
        for i, row in enumerate(rows):
            hits.append(
                {
                    "path": row.virtual_path if hasattr(row, "virtual_path") else row[6],
                    "chunk_index": row.chunk_index if hasattr(row, "chunk_index") else row[1],
                    "chunk_text": row.chunk_text if hasattr(row, "chunk_text") else row[0],
                    "score": round(1.0 - (i * 0.05), 4),
                    "start_offset": row.start_offset if hasattr(row, "start_offset") else row[2],
                    "end_offset": row.end_offset if hasattr(row, "end_offset") else row[3],
                    "line_start": row.line_start if hasattr(row, "line_start") else row[4],
                    "line_end": row.line_end if hasattr(row, "line_end") else row[5],
                }
            )

        # R5 review: ReBAC-filter the results when an enforcer is wired.
        # Fail closed when permissions must be enforced but no valid context
        # was supplied — callers that can legitimately bypass (admin/internal)
        # use ``enforce_permissions=False`` at SearchService construction.
        if self._permission_enforcer is not None and hits:
            if context is None:
                if self._enforce_permissions:
                    logger.warning(
                        "[SearchService] SQL chunk fallback called without OperationContext "
                        "while permissions are enforced — returning empty result (fail-closed)."
                    )
                    return []
            else:
                all_paths = [h["path"] for h in hits]
                accessible = set(self._permission_enforcer.filter_list(all_paths, context))
                hits = [h for h in hits if h["path"] in accessible]
        return hits

    @rpc_expose(description="Index documents for semantic search")
    async def semantic_search_index(
        self,
        path: str = "/",
        recursive: bool = True,
    ) -> dict[str, int]:
        """Index documents for semantic search.

        Args:
            path: Path to index (file or directory)
            recursive: If True, index directory recursively

        Returns:
            Dictionary mapping file paths to number of chunks indexed

        Raises:
            ValueError: If semantic search is not initialized
        """
        # Prefer IndexingService (Issue #2075)
        if self._indexing_service is not None:
            try:
                num_chunks = await self._indexing_service.index_document(path)
                return {path: num_chunks}
            except ValueError:
                # path is a directory or doesn't exist as single file
                pass

            if recursive:
                idx_results = await self._indexing_service.index_directory(path)
                return {p: r.chunks_indexed for p, r in idx_results.items()}
            return {}

        # Fallback: pipeline-based bulk indexing (RPC path without nx)
        if self._pipeline_indexer is not None:
            return await self._pipeline_indexer.index_path(path, recursive)
        return {}

    @rpc_expose(description="Get semantic search indexing statistics")
    async def semantic_search_stats(self) -> dict[str, Any]:
        """Get semantic search indexing statistics."""
        daemon = getattr(self, "_search_daemon", None)
        if daemon is not None:
            stats = dict(daemon.get_stats())
            stats.setdefault("engine", stats.get("backend", "txtai"))
            return stats

        if self._indexing_service is not None:
            return await self._indexing_service.get_index_stats()

        # SQL fallback when indexing_service is unavailable (Issue #2663)
        if self._record_store is not None:
            return self._sql_chunk_stats()

        raise ValueError(
            "Semantic search is not available. No indexing service or record store configured."
        )

    def _sql_chunk_stats(self) -> dict[str, Any]:
        """Basic stats from document_chunks table."""
        from sqlalchemy import text as sa_text

        assert self._record_store is not None  # caller checks
        try:
            session = self._record_store.session_factory()
            try:
                total_chunks = (
                    session.execute(sa_text("SELECT count(*) FROM document_chunks")).scalar() or 0
                )
                total_files = (
                    session.execute(
                        sa_text("SELECT count(DISTINCT path_id) FROM document_chunks")
                    ).scalar()
                    or 0
                )
                return {
                    "total_chunks": total_chunks,
                    "total_files": total_files,
                    "engine": "sql_fallback",
                }
            finally:
                session.close()
        except Exception as e:
            logger.warning("SQL chunk stats failed: %s", e)
            return {"total_chunks": 0, "total_files": 0, "engine": "sql_fallback"}

    @rpc_expose(description="Initialize semantic search engine")
    async def initialize_semantic_search(
        self,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        chunk_size: int = 1024,
        chunk_strategy: str = "semantic",
        async_mode: bool = True,  # noqa: ARG002
        contextual_chunking: bool = False,  # noqa: ARG002
        context_generator: Any | None = None,  # noqa: ARG002
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine with embedding provider (RPC path).

        Delegates to factory helper for component creation (Issue #2075, DRY).
        """
        from nexus.factory._semantic_search import create_semantic_search_components

        if self._record_store is None:
            raise RuntimeError("Semantic search requires RecordStore (SQL engine)")

        components = await create_semantic_search_components(
            record_store=self._record_store,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            api_key=api_key,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            cache_url=cache_url,
            embedding_cache_ttl=embedding_cache_ttl,
            # RPC-path extras for PipelineIndexer
            session_factory=self._gw_session_factory,
            metadata=self.metadata,
            file_reader=self._read,
            file_lister=self.list,
        )
        self._query_service = components.query_service
        self._indexing_service = components.indexing_service
        self._indexing_pipeline = components.indexing_pipeline
        self._pipeline_indexer = components.pipeline_indexer
