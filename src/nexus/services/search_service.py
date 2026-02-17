"""Search Service - Extracted from NexusFSSearchMixin (Issue #1287).

This service handles all search operations:
- File listing with pagination and permission filtering (via SearchListingMixin)
- Glob pattern matching with adaptive algorithms
- Content searching (grep) with 5 strategies (via SearchGrepMixin)
- Semantic search with embeddings (via SemanticSearchMixin)

Extracted from: nexus_fs_search.py (2,817 lines)
"""

from __future__ import annotations

import asyncio
import builtins
import fnmatch
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast

from cachetools import TTLCache

from nexus.core import glob_fast, grep_fast, trigram_fast
from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose
from nexus.search.strategies import (
    GLOB_RUST_THRESHOLD,
    GREP_CACHED_TEXT_RATIO,
    GREP_PARALLEL_THRESHOLD,
    GREP_PARALLEL_WORKERS,
    GREP_SEQUENTIAL_THRESHOLD,
    GREP_TRIGRAM_THRESHOLD,
    GREP_ZOEKT_THRESHOLD,
    GlobStrategy,
    SearchStrategy,
)
from nexus.services.gateway import NexusFSGateway
from nexus.services.search_grep_mixin import SearchGrepMixin
from nexus.services.search_listing_mixin import SearchListingMixin
from nexus.services.search_semantic import SemanticSearchMixin

# List directory traversal thresholds (Issue #901)
LIST_PARALLEL_WORKERS = 10  # Thread pool size for parallel directory listing (I/O-bound)

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
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter
    from nexus.rebac.enforcer import PermissionEnforcer
    from nexus.rebac.manager import EnhancedReBACManager


class SearchService(SearchListingMixin, SearchGrepMixin, SemanticSearchMixin):
    """Independent search service extracted from NexusFS.

    Handles file listing, glob matching, grep, and semantic search.
    Listing methods are provided by SearchListingMixin.
    Grep methods are provided by SearchGrepMixin.
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

        # Bounded TTL cache for cross-zone sharing queries (Issue #904)
        self._cross_zone_cache: TTLCache[tuple[str, ...], builtins.list[str]] = TTLCache(
            maxsize=1024, ttl=5.0
        )

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
        zone_id: str | None = None,
    ) -> SearchStrategy:
        """Select optimal grep strategy (Issue #929, #954)."""
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
