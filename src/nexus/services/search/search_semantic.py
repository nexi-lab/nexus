"""Semantic Search Mixin - Extracted from SearchService (Issue #1287).

Thin facade that delegates to IndexingService + QueryService (Issue #2075).

Provides all semantic search functionality:
- Natural language search with embeddings
- Document indexing for semantic search
- Search statistics
- Initialization of embedding providers
"""

import builtins
import logging
from typing import TYPE_CHECKING, Any

from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.bricks.search.indexing_service import IndexingService
    from nexus.bricks.search.pipeline_indexer import PipelineIndexer

    # Removed: txtai handles this (Issue #2663)
    # from nexus.bricks.search.query_service import QueryService
    QueryService = Any


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


class SemanticSearchMixin:
    """Mixin providing semantic search capabilities for SearchService.

    Delegates to:
        _query_service: QueryService for search execution
        _indexing_service: IndexingService for document indexing (when file_reader available)
        _pipeline_indexer: PipelineIndexer for bulk indexing (RPC path without nx)

    Methods provided:
        - ainitialize_semantic_search: Factory init from NexusFS
        - initialize_semantic_search: RPC-exposed init
        - semantic_search: Natural language search
        - semantic_search_index: Document indexing
        - semantic_search_stats: Indexing statistics
    """

    @property
    def _has_search_engine(self) -> bool:
        """Check if a search engine is available."""
        return hasattr(self, "_query_service") and self._query_service is not None

    def _require_search_engine(self) -> None:
        """Raise ValueError if no search engine is initialized."""
        if not self._has_search_engine:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await search.initialize_semantic_search()"
            )

    # Type hints for attributes provided by SearchService.__init__
    _query_service: "QueryService | None"
    _indexing_service: "IndexingService | None"
    _indexing_pipeline: Any  # IndexingPipeline | None
    _pipeline_indexer: "PipelineIndexer | None"
    _record_store: Any
    _gw_session_factory: Any
    _gw_backend: Any
    metadata: Any  # MetastoreABC, provided by SearchService
    _read: Any  # Callable, provided by SearchService
    list: Any  # Callable, provided by SearchService

    # =========================================================================
    # Semantic Search Initialization (Issue #1287, #2075)
    # =========================================================================

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

    # =========================================================================
    # Public API: Semantic Search
    # =========================================================================

    @rpc_expose(description="Search documents using natural language queries")
    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,  # noqa: ARG002
        search_mode: str = "semantic",
        adaptive_k: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """Search documents using natural language queries.

        Args:
            query: Natural language query
            path: Root path to search
            limit: Maximum number of results
            filters: Optional filters (currently unused)
            search_mode: "keyword", "semantic", or "hybrid"
            adaptive_k: If True, dynamically adjust limit (Issue #1021)

        Raises:
            ValueError: If semantic search is not initialized
        """
        self._require_search_engine()

        if self._query_service is None:
            raise ValueError("Semantic search not properly initialized")

        results = await self._query_service.search(
            query=query,
            path=path,
            limit=limit,
            search_mode=search_mode,
            adaptive_k=adaptive_k,
        )
        return [_result_to_dict(r) for r in results]

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
        self._require_search_engine()

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
        """Get semantic search indexing statistics.

        Raises:
            ValueError: If semantic search is not initialized
        """
        self._require_search_engine()

        if self._indexing_service is not None:
            return await self._indexing_service.get_index_stats()

        raise ValueError("Semantic search not properly initialized")

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
