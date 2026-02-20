"""Semantic Search Mixin - Extracted from SearchService (Issue #1287).

Thin facade that delegates to IndexingService + QueryService (Issue #2075).

Provides all semantic search functionality:
- Natural language search with embeddings
- Document indexing for semantic search
- Search statistics
- Initialization of embedding providers
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.search.indexing_service import IndexingService
    from nexus.search.query_service import QueryService


class SemanticSearchMixin:
    """Mixin providing semantic search capabilities for SearchService.

    Delegates to:
        _query_service: QueryService for search execution
        _indexing_service: IndexingService for document indexing (when file_reader available)
        _indexing_pipeline: IndexingPipeline for bulk indexing (RPC path without nx)

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
        return (
            (hasattr(self, "_query_service") and self._query_service is not None)
            or (hasattr(self, "_async_search") and self._async_search is not None)
            or (hasattr(self, "_semantic_search") and self._semantic_search is not None)
        )

    def _require_search_engine(self) -> None:
        """Raise ValueError if no search engine is initialized."""
        if not self._has_search_engine:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await search.initialize_semantic_search()"
            )

    # Type hints for attributes provided by SearchService.__init__
    _query_service: QueryService | None
    _indexing_service: IndexingService | None
    _indexing_pipeline: Any  # IndexingPipeline | None
    _async_search: Any  # kept for backward compat during migration
    _semantic_search: Any  # kept for backward compat during migration
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
        record_store_engine: Any,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        chunk_size: int = 1024,
        chunk_strategy: str = "semantic",
        async_mode: bool = True,  # noqa: ARG002
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine.

        Creates IndexingService + QueryService backed by IndexingPipeline,
        VectorDatabase, and FileReaderProtocol.

        Args:
            nx: NexusFS instance (used to create FileReaderProtocol adapter)
            record_store_engine: SQLAlchemy engine from RecordStore
            embedding_provider: Provider name (e.g., "openai", "voyage")
            embedding_model: Model name for embeddings
            api_key: API key for embedding provider
            chunk_size: Chunk size in tokens
            chunk_strategy: Chunking strategy ("fixed", "semantic", "overlapping")
            async_mode: Unused — kept for API compat.
            cache_url: Redis/Dragonfly URL for embedding cache
            embedding_cache_ttl: Cache TTL in seconds (default: 3 days)
        """

        from nexus.search.chunking import ChunkStrategy, DocumentChunker
        from nexus.search.indexing import IndexingPipeline
        from nexus.search.indexing_service import IndexingService
        from nexus.search.query_service import QueryService
        from nexus.search.vector_db import VectorDatabase

        # --- Embedding provider ---
        emb_provider = None
        if embedding_provider:
            from nexus.lib.env import get_dragonfly_url
            from nexus.search.embeddings import create_cached_embedding_provider

            effective_cache_url = cache_url or get_dragonfly_url()
            emb_provider = await create_cached_embedding_provider(
                provider=embedding_provider,
                model=embedding_model,
                api_key=api_key,
                cache_url=effective_cache_url,
                cache_ttl=embedding_cache_ttl,
            )

        strategy_map = {
            "fixed": ChunkStrategy.FIXED,
            "semantic": ChunkStrategy.SEMANTIC,
            "overlapping": ChunkStrategy.OVERLAPPING,
        }
        chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

        # --- Core components ---
        vector_db = VectorDatabase(record_store_engine)
        vector_db.initialize()

        chunker = DocumentChunker(
            chunk_size=chunk_size,
            strategy=chunk_strat,
            overlap_size=128,
        )

        _sync_sf = self._record_store.session_factory if self._record_store is not None else None
        _async_sf = None
        if self._record_store is not None:
            with contextlib.suppress(NotImplementedError, AttributeError):
                _async_sf = self._record_store.async_session_factory

        pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=emb_provider,
            db_type=vector_db.db_type,
            async_session_factory=_async_sf,
            max_concurrency=10,
            cross_doc_batching=True,
        )
        self._indexing_pipeline = pipeline

        # --- QueryService ---
        if _sync_sf is not None:
            self._query_service = QueryService(
                vector_db=vector_db,
                session_factory=_sync_sf,
                embedding_provider=emb_provider,
            )
        else:
            self._query_service = None

        # --- IndexingService (needs file_reader from nx) ---
        from nexus.factory import _NexusFSFileReader

        _file_reader = _NexusFSFileReader(nx) if nx is not None else None

        if _file_reader is not None and _sync_sf is not None:
            self._indexing_service = IndexingService(
                pipeline=pipeline,
                file_reader=_file_reader,
                session_factory=_sync_sf,
                vector_db=vector_db,
                embedding_provider=emb_provider,
            )
        else:
            self._indexing_service = None

        # Legacy attributes — set to None so _has_search_engine doesn't
        # need them and old code that checks them won't crash.
        self._async_search = None
        self._semantic_search = None

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

        # Prefer QueryService (Issue #2075)
        if self._query_service is not None:
            results = await self._query_service.search(
                query=query,
                path=path,
                limit=limit,
                search_mode=search_mode,
                adaptive_k=adaptive_k,
            )
            return [
                {
                    "path": r.path,
                    "chunk_index": r.chunk_index,
                    "chunk_text": r.chunk_text,
                    "score": r.score,
                    "start_offset": r.start_offset,
                    "end_offset": r.end_offset,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                }
                for r in results
            ]

        # Fallback: AsyncSemanticSearch (legacy — will be removed)
        if hasattr(self, "_async_search") and self._async_search is not None:
            results_legacy = await self._async_search.search(
                query=query,
                limit=limit,
                path_filter=path if path != "/" else None,
                search_mode=search_mode,
                adaptive_k=adaptive_k,
            )
            return [
                {
                    "path": r.path,
                    "chunk_index": r.chunk_index,
                    "chunk_text": r.chunk_text,
                    "score": r.score,
                    "start_offset": r.start_offset,
                    "end_offset": r.end_offset,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                }
                for r in results_legacy
            ]

        raise ValueError("Semantic search not properly initialized")

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
        return await self._pipeline_index_documents(path, recursive)

    @rpc_expose(description="Get semantic search indexing statistics")
    async def semantic_search_stats(self) -> dict[str, Any]:
        """Get semantic search indexing statistics.

        Raises:
            ValueError: If semantic search is not initialized
        """
        self._require_search_engine()

        if self._indexing_service is not None:
            return await self._indexing_service.get_index_stats()

        # Fallback: legacy
        if hasattr(self, "_async_search") and self._async_search is not None:
            result: dict[str, Any] = await self._async_search.get_stats()
            return result

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
        contextual_chunking: bool = False,  # noqa: ARG002
        context_generator: Any | None = None,  # noqa: ARG002
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine with embedding provider (RPC path).

        This path does NOT have access to NexusFS, so IndexingService cannot be
        created. QueryService is created for search; bulk indexing uses
        IndexingPipeline directly.
        """

        from nexus.search.chunking import ChunkStrategy, DocumentChunker
        from nexus.search.indexing import IndexingPipeline
        from nexus.search.query_service import QueryService
        from nexus.search.vector_db import VectorDatabase

        emb_provider = None
        if embedding_provider:
            from nexus.lib.env import get_dragonfly_url
            from nexus.search.embeddings import create_cached_embedding_provider

            effective_cache_url = cache_url or get_dragonfly_url()
            emb_provider = await create_cached_embedding_provider(
                provider=embedding_provider,
                model=embedding_model,
                api_key=api_key,
                cache_url=effective_cache_url,
                cache_ttl=embedding_cache_ttl,
            )

        strategy_map = {
            "fixed": ChunkStrategy.FIXED,
            "semantic": ChunkStrategy.SEMANTIC,
            "overlapping": ChunkStrategy.OVERLAPPING,
        }
        chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

        if self._record_store is None:
            raise RuntimeError("Semantic search requires RecordStore (SQL engine)")

        engine = self._record_store.engine
        vector_db = VectorDatabase(engine)
        vector_db.initialize()

        chunker = DocumentChunker(
            chunk_size=chunk_size,
            strategy=chunk_strat,
            overlap_size=128,
        )

        _sync_sf = self._record_store.session_factory
        _async_sf = None
        with contextlib.suppress(NotImplementedError, AttributeError):
            _async_sf = self._record_store.async_session_factory

        if not async_mode:
            raise NotImplementedError(
                "Sync semantic search requires NexusFS integration. Use async_mode=True."
            )

        self._indexing_pipeline = IndexingPipeline(
            chunker=chunker,
            embedding_provider=emb_provider,
            db_type=vector_db.db_type,
            async_session_factory=_async_sf,
            max_concurrency=10,
            cross_doc_batching=True,
        )

        self._query_service = QueryService(
            vector_db=vector_db,
            session_factory=_sync_sf,
            embedding_provider=emb_provider,
        )

        # No file_reader available in RPC path — IndexingService not created.
        self._indexing_service = None
        self._async_search = None
        self._semantic_search = None

    # =========================================================================
    # Helper Methods
    # =========================================================================

    async def _pipeline_index_documents(
        self,
        path: str,
        recursive: bool,
    ) -> dict[str, int]:
        """Index documents using IndexingPipeline directly (RPC path)."""
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        files_to_index: list[str] = []
        try:
            await asyncio.to_thread(self._read, path)
            files_to_index = [path]
        except Exception:
            file_list = await asyncio.to_thread(self.list, path, recursive)
            if hasattr(file_list, "items"):
                file_list = file_list.items
            for item in file_list:
                file_path = item if isinstance(item, str) else item.get("path", "")
                if file_path and not file_path.endswith("/"):
                    files_to_index.append(file_path)

        if not files_to_index:
            return {}

        if self._gw_session_factory is None:
            logger.warning("session_factory not provided, cannot index documents")
            return {}

        def _prepare_documents_sync() -> list[tuple[str, str, str]]:
            docs: list[tuple[str, str, str]] = []
            with self._gw_session_factory() as session:
                for fp in files_to_index:
                    try:
                        content = self.metadata.get_searchable_text(fp)
                        if content is None:
                            content_raw = self._read(fp)
                            if isinstance(content_raw, bytes):
                                content = content_raw.decode("utf-8", errors="ignore")
                            else:
                                content = str(content_raw)
                        stmt = select(FilePathModel).where(
                            FilePathModel.virtual_path == fp,
                            FilePathModel.deleted_at.is_(None),
                        )
                        result = session.execute(stmt)
                        file_model = result.scalar_one_or_none()
                        if file_model and content:
                            docs.append((fp, content, file_model.path_id))
                    except Exception as e:
                        logger.warning("Failed to prepare %s for indexing: %s", fp, e)
            return docs

        documents = await asyncio.to_thread(_prepare_documents_sync)
        if not documents:
            return {}

        pipeline = getattr(self, "_indexing_pipeline", None)
        if pipeline is None:
            logger.warning("No indexing pipeline configured")
            return {}

        idx_results = await pipeline.index_documents(documents)
        return {r.path: r.chunks_indexed for r in idx_results}
