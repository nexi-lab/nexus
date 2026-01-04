"""Search Service - Extracted from NexusFSSearchMixin.

This service handles all search operations:
- File listing with pagination
- Glob pattern matching
- Content searching (grep) with adaptive algorithms
- Semantic search with embeddings

Phase 2: Core Refactoring (Issue #988, Task 2.1)
Extracted from: nexus_fs_search.py (2,361 lines)
"""

from __future__ import annotations

import builtins
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose

# =============================================================================
# Adaptive Algorithm Selection Configuration (Issue #929)
# =============================================================================

# Grep strategy thresholds
GREP_SEQUENTIAL_THRESHOLD = 10  # Below this file count, use sequential
GREP_PARALLEL_THRESHOLD = 100  # Above this, consider parallel processing
GREP_ZOEKT_THRESHOLD = 1000  # Above this, prefer Zoekt if available
GREP_PARALLEL_WORKERS = 4  # Thread pool size for parallel grep
GREP_CACHED_TEXT_RATIO = 0.8  # Use cached text if > 80% have cached text

# Glob strategy thresholds
GLOB_RUST_THRESHOLD = 50  # Use Rust acceleration above this file count


class SearchStrategy(StrEnum):
    """Strategy for grep operations (Issue #929).

    Selected at runtime based on file count, cached text ratio, and backends.
    """

    SEQUENTIAL = "sequential"  # < 10 files - no parallelization overhead
    CACHED_TEXT = "cached_text"  # > 80% files have pre-parsed text
    RUST_BULK = "rust_bulk"  # 10-1000 files with Rust available
    PARALLEL_POOL = "parallel_pool"  # 100-10000 files, parallel processing
    ZOEKT_INDEX = "zoekt_index"  # > 1000 files with Zoekt index


class GlobStrategy(StrEnum):
    """Strategy for glob operations (Issue #929)."""

    FNMATCH_SIMPLE = "fnmatch_simple"  # Simple patterns without **
    REGEX_COMPILED = "regex_compiled"  # Complex patterns with **
    RUST_BULK = "rust_bulk"  # > 50 files with Rust available
    DIRECTORY_PRUNED = "directory_pruned"  # Pattern has static prefix


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.metadata import PaginatedResult
    from nexus.core.mount_router import MountRouter
    from nexus.core.permissions import OperationContext, PermissionEnforcer
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager
    from nexus.search.async_search import AsyncSemanticSearch
    from nexus.search.semantic import SemanticSearch
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class SearchService:
    """Independent search service extracted from NexusFS.

    Handles all file discovery and search operations:
    - File listing with pagination
    - Glob pattern matching
    - Content searching (grep)
    - Semantic search with embeddings

    Uses adaptive algorithm selection (Issue #929) to choose optimal
    strategies based on data characteristics.

    Architecture:
        - No direct filesystem dependencies
        - Pure data processing and algorithms
        - Dependency injection for stores and backends

    Example:
        ```python
        search = SearchService(
            metadata_store=metadata,
            permission_enforcer=permissions,
            router=router
        )

        # List files
        results = await search.list(path="/docs", recursive=True)

        # Glob search
        files = await search.glob(pattern="**/*.py", path="/src")

        # Content search
        matches = await search.grep(pattern="TODO", path="/")

        # Semantic search
        results = await search.semantic_search(
            query="How does authentication work?",
            limit=10
        )
        ```
    """

    def __init__(
        self,
        metadata_store: SQLAlchemyMetadataStore,
        permission_enforcer: PermissionEnforcer | None = None,
        router: MountRouter | None = None,
        rebac_manager: EnhancedReBACManager | None = None,
        enforce_permissions: bool = True,
        default_context: OperationContext | None = None,
    ):
        """Initialize search service.

        Args:
            metadata_store: Metadata store for file information
            permission_enforcer: Permission enforcer for access control
            router: Mount router for backend operations
            rebac_manager: ReBAC manager for relationship-based permissions
            enforce_permissions: Whether to enforce permission checks
            default_context: Default operation context (embedded mode)
        """
        self.metadata = metadata_store
        self._permission_enforcer = permission_enforcer
        self.router = router
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._default_context = default_context

        # Semantic search (initialized later)
        self._semantic_search: SemanticSearch | None = None
        self._async_search: AsyncSemanticSearch | None = None

        logger.info("[SearchService] Initialized")

    # =========================================================================
    # Public API: File Listing
    # =========================================================================

    @rpc_expose(description="List files in a directory")
    async def list(
        self,
        path: str = "/",
        recursive: bool = False,
        context: Any = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
    ) -> PaginatedResult | builtins.list[dict[str, Any]]:
        """List files in a directory with optional pagination.

        Args:
            path: Directory path to list
            recursive: If True, list recursively
            context: Operation context for permissions
            limit: Maximum number of results (None = no limit)
            offset: Number of results to skip
            filters: Optional filters (prefix, file_type, etc.)

        Returns:
            List of file info dicts or PaginatedResult if paginated

        Raises:
            PermissionDeniedError: If user lacks read permission
        """
        # TODO: Extract list implementation from NexusFSSearchMixin.list()
        raise NotImplementedError("list() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Public API: Glob Pattern Matching
    # =========================================================================

    @rpc_expose(description="Find files matching glob pattern")
    async def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching glob pattern with adaptive algorithm selection.

        Supports patterns:
        - *: Match any characters in one path segment
        - **: Match any path recursively
        - ?: Match single character
        - [abc]: Match character set

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "test_*.txt")
            path: Root path to search from
            context: Operation context for permissions

        Returns:
            List of matching file paths

        Examples:
            # Find all Python files
            files = await search.glob("**/*.py")

            # Find test files in src/
            tests = await search.glob("test_*.py", path="/src")

        Raises:
            PermissionDeniedError: If user lacks read permission
        """
        # TODO: Extract glob implementation
        raise NotImplementedError("glob() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Public API: Content Searching (Grep)
    # =========================================================================

    @rpc_expose(description="Search file contents using regex")
    async def grep(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,
        case_sensitive: bool = True,
        max_results: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex with adaptive algorithm selection.

        Automatically selects optimal strategy (Issue #929):
        - Sequential: < 10 files
        - Cached Text: > 80% files have cached text
        - Rust Bulk: 10-1000 files with Rust available
        - Parallel Pool: 100-10000 files, CPU-bound
        - Zoekt Index: > 1000 files with Zoekt index

        Args:
            pattern: Regular expression pattern
            path: Root path to search
            context: Operation context for permissions
            case_sensitive: If True, case-sensitive matching
            max_results: Maximum number of results

        Returns:
            List of match dicts with:
            - path: File path
            - line_number: Line number (1-indexed)
            - line_text: Line content
            - match_start: Start column of match
            - match_end: End column of match

        Examples:
            # Find all TODOs
            todos = await search.grep(r"TODO|FIXME", path="/src")

            # Case-insensitive search
            results = await search.grep(r"error", case_sensitive=False)

        Raises:
            PermissionDeniedError: If user lacks read permission
        """
        # TODO: Extract grep implementation
        raise NotImplementedError("grep() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Public API: Semantic Search
    # =========================================================================

    @rpc_expose(description="Search documents using natural language queries")
    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
    ) -> builtins.list[dict[str, Any]]:
        """Search documents using natural language queries.

        Supports three search modes:
        - "keyword": Fast keyword search using FTS (no embeddings)
        - "semantic": Semantic search using vector embeddings
        - "hybrid": Combines keyword + semantic for best results

        Args:
            query: Natural language query (e.g., "How does authentication work?")
            path: Root path to search
            limit: Maximum number of results
            filters: Optional filters (file_type, etc.)
            search_mode: "keyword", "semantic", or "hybrid"

        Returns:
            List of search result dicts with:
            - path: File path
            - chunk_index: Index of chunk in document
            - chunk_text: Text content of chunk
            - score: Relevance score (0.0 to 1.0)
            - start_offset: Start offset in document (optional)
            - end_offset: End offset in document (optional)

        Examples:
            # Search for authentication info
            results = await search.semantic_search(
                "How does authentication work?"
            )

            # Search in specific directory
            results = await search.semantic_search(
                "database migration",
                path="/docs",
                limit=5
            )

            # Hybrid search (best results)
            results = await search.semantic_search(
                "error handling",
                search_mode="hybrid"
            )

        Raises:
            ValueError: If semantic search is not initialized
            PermissionDeniedError: If user lacks read permission
        """
        # Check if either async or sync search is initialized
        has_async = hasattr(self, "_async_search") and self._async_search is not None
        has_sync = hasattr(self, "_semantic_search") and self._semantic_search is not None

        if not has_async and not has_sync:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await search.initialize_semantic_search()"
            )

        # Use async search for non-blocking DB operations (high throughput)
        if has_async:
            assert self._async_search is not None  # Type guard for mypy
            results = await self._async_search.search(
                query=query,
                limit=limit,
                path_filter=path if path != "/" else None,
                search_mode=search_mode,
            )
            return [
                {
                    "path": result.path,
                    "chunk_index": result.chunk_index,
                    "chunk_text": result.chunk_text,
                    "score": result.score,
                    "start_offset": result.start_offset,
                    "end_offset": result.end_offset,
                    "line_start": result.line_start,
                    "line_end": result.line_end,
                }
                for result in results
            ]

        # Fallback to sync search (requires NexusFS integration)
        if has_sync and self._semantic_search is not None:
            sync_results = await self._semantic_search.search(
                query=query, path=path, limit=limit, filters=filters, search_mode=search_mode
            )

            return [
                {
                    "path": result.path,
                    "chunk_index": result.chunk_index,
                    "chunk_text": result.chunk_text,
                    "score": result.score,
                    "start_offset": result.start_offset,
                    "end_offset": result.end_offset,
                    "line_start": result.line_start,
                    "line_end": result.line_end,
                }
                for result in sync_results
            ]

        # Should not reach here due to initialization check above
        raise ValueError("Semantic search not properly initialized")

    @rpc_expose(description="Index documents for semantic search")
    async def semantic_search_index(
        self, path: str = "/", recursive: bool = True
    ) -> dict[str, int]:
        """Index documents for semantic search.

        Chunks documents and generates embeddings. Must run before
        using semantic_search().

        Args:
            path: Path to index (file or directory)
            recursive: If True, index directory recursively

        Returns:
            Dictionary mapping file paths to number of chunks indexed

        Examples:
            # Index all documents
            await search.semantic_search_index()

            # Index specific directory
            await search.semantic_search_index("/docs")

        Raises:
            ValueError: If semantic search is not initialized
        """
        # Check if either async or sync search is initialized
        has_async = hasattr(self, "_async_search") and self._async_search is not None
        has_sync = hasattr(self, "_semantic_search") and self._semantic_search is not None

        if not has_async and not has_sync:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await search.initialize_semantic_search()"
            )

        # Use async indexing for high throughput
        if has_async:
            assert self._async_search is not None  # Type guard for mypy
            return await self._async_index_documents(path, recursive)

        # Fallback to sync indexing (requires NexusFS integration)
        raise NotImplementedError("Sync indexing requires NexusFS integration - use async mode")

    @rpc_expose(description="Get semantic search indexing statistics")
    async def semantic_search_stats(self) -> dict[str, Any]:
        """Get semantic search indexing statistics.

        Returns:
            Dictionary with:
            - total_chunks: Total indexed chunks
            - indexed_files: Number of indexed files
            - collection_name: Vector collection name
            - embedding_model: Embedding model name
            - chunk_size: Chunk size in tokens
            - chunk_strategy: Chunking strategy

        Raises:
            ValueError: If semantic search is not initialized
        """
        # Check if either async or sync search is initialized
        has_async = hasattr(self, "_async_search") and self._async_search is not None
        has_sync = hasattr(self, "_semantic_search") and self._semantic_search is not None

        if not has_async and not has_sync:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await search.initialize_semantic_search()"
            )

        # Prefer async search
        if has_async:
            assert self._async_search is not None  # Type guard for mypy
            return await self._async_search.get_stats()

        # Fallback to sync search
        if has_sync and self._semantic_search is not None:
            return await self._semantic_search.get_index_stats()

        # Should not reach here
        raise ValueError("Semantic search not properly initialized")

    @rpc_expose(description="Initialize semantic search engine")
    async def initialize_semantic_search(
        self,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        chunk_size: int = 1024,
        chunk_strategy: str = "semantic",
        async_mode: bool = True,
    ) -> None:
        """Initialize semantic search engine with embedding provider.

        Args:
            embedding_provider: "openai", "cohere", "huggingface", etc.
            embedding_model: Model name (provider-specific)
            api_key: API key for embedding provider
            chunk_size: Chunk size in tokens
            chunk_strategy: "semantic", "fixed", or "recursive"
            async_mode: Use async backend for high throughput

        Examples:
            # Initialize with OpenAI
            await search.initialize_semantic_search(
                embedding_provider="openai",
                embedding_model="text-embedding-3-small",
                api_key=os.getenv("OPENAI_API_KEY")
            )
        """
        from nexus.search.chunking import ChunkStrategy

        # Create embedding provider (optional)
        emb_provider = None
        if embedding_provider:
            from nexus.search.embeddings import create_embedding_provider

            emb_provider = create_embedding_provider(
                provider=embedding_provider, model=embedding_model, api_key=api_key
            )

        # Map string to enum
        strategy_map = {
            "fixed": ChunkStrategy.FIXED,
            "semantic": ChunkStrategy.SEMANTIC,
            "overlapping": ChunkStrategy.OVERLAPPING,
        }
        chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

        # Get database URL from metadata store
        database_url = str(self.metadata.engine.url)

        if async_mode:
            # Use async search for high-throughput (non-blocking DB operations)
            from nexus.search.async_search import AsyncSemanticSearch

            self._async_search = AsyncSemanticSearch(
                database_url=database_url,
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
            )
            await self._async_search.initialize()

            # Note: In async mode, we use _async_search instead of _semantic_search
            # _semantic_search remains None since it requires NexusFS instance
            # All semantic search methods check _async_search first
        else:
            # Sync mode not supported in service extraction
            raise NotImplementedError(
                "Sync semantic search requires NexusFS integration. Use async_mode=True."
            )

    # =========================================================================
    # Helper Methods: Semantic Search Indexing
    # =========================================================================

    async def _async_index_documents(self, _path: str, _recursive: bool) -> dict[str, int]:
        """Index documents using async backend for high throughput.

        Note: This implementation requires NexusFS for file reading.
        In service extraction, this will need to be provided via dependency injection.

        Args:
            _path: Path to index (unused in stub)
            _recursive: Index recursively (unused in stub)

        Returns:
            Dict mapping path to number of chunks
        """
        raise NotImplementedError(
            "Document indexing requires NexusFS integration for file reading. "
            "This will be implemented when NexusFS uses composition pattern."
        )

    # =========================================================================
    # Helper Methods: Algorithm Selection (Issue #929)
    # =========================================================================

    def _select_grep_strategy(
        self, file_count: int, cached_text_ratio: float, has_zoekt: bool
    ) -> SearchStrategy:
        """Select optimal grep strategy based on data characteristics.

        Issue #929: Adaptive algorithm selection inspired by ClickHouse.

        Args:
            file_count: Number of files to search
            cached_text_ratio: Ratio of files with cached text (0.0-1.0)
            has_zoekt: Whether Zoekt index is available

        Returns:
            SearchStrategy to use
        """
        # TODO: Extract strategy selection logic
        raise NotImplementedError("Strategy selection not yet implemented")

    def _select_glob_strategy(self, pattern: str, file_count: int, has_rust: bool) -> GlobStrategy:
        """Select optimal glob strategy based on pattern and file count.

        Args:
            pattern: Glob pattern
            file_count: Estimated number of files
            has_rust: Whether Rust acceleration is available

        Returns:
            GlobStrategy to use
        """
        # TODO: Extract strategy selection logic
        raise NotImplementedError("Strategy selection not yet implemented")

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

        Args:
            path: Path to validate

        Returns:
            Normalized path

        Raises:
            ValueError: If path is invalid
        """
        # Normalize path
        if not path.startswith("/"):
            path = "/" + path

        # Remove trailing slash unless root
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        return path


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Skeleton created ✅
#
# TODO (in order of priority):
# 1. [ ] Extract list() method and helpers from NexusFSSearchMixin
# 2. [ ] Extract glob() method and strategy selection
# 3. [ ] Extract grep() method and parallel processing
# 4. [ ] Extract semantic_search() and related methods
# 5. [ ] Extract helper methods (_list_paginated, etc.)
# 6. [ ] Add unit tests for SearchService
# 7. [ ] Update NexusFS to use composition
# 8. [ ] Add backward compatibility shims with deprecation warnings
# 9. [ ] Update documentation and migration guide
#
# Lines extracted: 0 / 2,361 (0%)
# Files affected: 1 created, 0 modified
#
# This is a phased extraction to maintain working code at each step.
#
