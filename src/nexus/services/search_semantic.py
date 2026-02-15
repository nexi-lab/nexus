"""Semantic Search Mixin - Extracted from SearchService (Issue #1287).

This mixin provides all semantic search functionality:
- Natural language search with embeddings
- Document indexing for semantic search
- Search statistics
- Initialization of embedding providers

Extracted from: search_service.py (2,478 lines -> under 2,000)
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.search.async_search import AsyncSemanticSearch
    from nexus.search.semantic import SemanticSearch


class SemanticSearchMixin:
    """Mixin providing semantic search capabilities for SearchService.

    Attributes managed by this mixin:
        _semantic_search: Sync semantic search instance
        _async_search: Async semantic search instance
        _record_store: RecordStoreABC for SQL engine (needed for semantic search)

    Methods provided:
        - ainitialize_semantic_search: Factory init from NexusFS
        - initialize_semantic_search: RPC-exposed init
        - semantic_search: Natural language search
        - semantic_search_index: Document indexing
        - semantic_search_stats: Indexing statistics
        - _async_index_documents: Async bulk indexing helper
    """

    # Type hints for attributes provided by SearchService.__init__
    _semantic_search: SemanticSearch | None
    _async_search: AsyncSemanticSearch | None
    _record_store: Any
    _gw_session_factory: Any
    _gw_backend: Any
    metadata: Any  # FileMetadataProtocol, provided by SearchService
    _read: Any  # Callable, provided by SearchService
    list: Any  # Callable, provided by SearchService

    # =========================================================================
    # Semantic Search Initialization (Issue #1287, moved from NexusFS)
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
        async_mode: bool = True,
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine.

        Factory method that creates and configures SemanticSearch and
        AsyncSemanticSearch instances. Moved from NexusFS god object.

        Args:
            nx: NexusFS instance (SemanticSearch still requires full nx reference)
            record_store_engine: SQLAlchemy engine from RecordStore
            embedding_provider: Provider name (e.g., "openai", "voyage")
            embedding_model: Model name for embeddings
            api_key: API key for embedding provider
            chunk_size: Chunk size in tokens
            chunk_strategy: Chunking strategy ("fixed", "semantic", "overlapping")
            async_mode: If True, also initialize AsyncSemanticSearch
            cache_url: Redis/Dragonfly URL for embedding cache
            embedding_cache_ttl: Cache TTL in seconds (default: 3 days)
        """
        import os

        from nexus.search.chunking import ChunkStrategy

        emb_provider = None
        if embedding_provider:
            from nexus.search.embeddings import create_cached_embedding_provider

            effective_cache_url = cache_url or os.environ.get("NEXUS_DRAGONFLY_URL")
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
        database_url = str(record_store_engine.url)

        if async_mode:
            from nexus.search.async_search import AsyncSemanticSearch
            from nexus.search.semantic import SemanticSearch

            self._async_search = AsyncSemanticSearch(
                database_url=database_url,
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
            )
            await self._async_search.initialize()

            self._semantic_search = SemanticSearch(
                nx=nx,
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
                engine=record_store_engine,
            )
            self._semantic_search.initialize()
        else:
            from nexus.search.semantic import SemanticSearch

            self._semantic_search = SemanticSearch(
                nx=nx,
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
                engine=record_store_engine,
            )
            self._semantic_search.initialize()
            self._async_search = None

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
        adaptive_k: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """Search documents using natural language queries.

        Supports three search modes:
        - "keyword": Fast keyword search using FTS (no embeddings)
        - "semantic": Semantic search using vector embeddings
        - "hybrid": Combines keyword + semantic for best results

        Args:
            query: Natural language query
            path: Root path to search
            limit: Maximum number of results
            filters: Optional filters (file_type, etc.)
            search_mode: "keyword", "semantic", or "hybrid"
            adaptive_k: If True, dynamically adjust limit (Issue #1021)

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
                adaptive_k=adaptive_k,
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
                query=query,
                path=path,
                limit=limit,
                filters=filters,
                search_mode=search_mode,
                adaptive_k=adaptive_k,
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

        # Fallback to sync indexing via _semantic_search
        assert self._semantic_search is not None  # Type guard
        try:
            await asyncio.to_thread(self._read, path)
            num_chunks = await self._semantic_search.index_document(path)
            return {path: num_chunks}
        except Exception:
            pass

        if recursive:
            return await self._semantic_search.index_directory(path)
        else:
            files_result = await asyncio.to_thread(self.list, path, False)
            files = files_result.items if hasattr(files_result, "items") else files_result
            results: dict[str, int] = {}
            for item in files:
                file_path = item["name"] if isinstance(item, dict) else item
                if not file_path.endswith("/"):
                    try:
                        num_chunks = await self._semantic_search.index_document(file_path)
                        results[file_path] = num_chunks
                    except Exception:
                        results[file_path] = -1
            return results

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
        contextual_chunking: bool = False,
        context_generator: Any | None = None,
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine with embedding provider.

        Args:
            embedding_provider: "openai", "cohere", "huggingface", etc.
            embedding_model: Model name (provider-specific)
            api_key: API key for embedding provider
            chunk_size: Chunk size in tokens
            chunk_strategy: "semantic", "fixed", or "recursive"
            async_mode: Use async backend for high throughput
            contextual_chunking: Enable contextual chunking (Anthropic pattern)
            context_generator: Callable for generating chunk context
            cache_url: Redis/Dragonfly URL for embedding cache
            embedding_cache_ttl: Cache TTL in seconds (default: 3 days)
        """
        import os

        from nexus.search.chunking import ChunkStrategy

        # Create embedding provider with caching
        emb_provider = None
        if embedding_provider:
            from nexus.search.embeddings import create_cached_embedding_provider

            effective_cache_url = cache_url or os.environ.get("NEXUS_DRAGONFLY_URL")
            emb_provider = await create_cached_embedding_provider(
                provider=embedding_provider,
                model=embedding_model,
                api_key=api_key,
                cache_url=effective_cache_url,
                cache_ttl=embedding_cache_ttl,
            )

        # Map string to enum
        strategy_map = {
            "fixed": ChunkStrategy.FIXED,
            "semantic": ChunkStrategy.SEMANTIC,
            "overlapping": ChunkStrategy.OVERLAPPING,
        }
        chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

        # Get database URL from record store (service dependency)
        if self._record_store is None:
            raise RuntimeError("Semantic search requires RecordStore (SQL engine)")
        database_url = str(self._record_store.engine.url)

        if async_mode:
            # Use async search for high-throughput (non-blocking DB operations)
            from nexus.search.async_search import AsyncSemanticSearch

            self._async_search = AsyncSemanticSearch(
                database_url=database_url,
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
                contextual_chunking=contextual_chunking,
                context_generator=context_generator,
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

    async def _async_index_documents(self, path: str, recursive: bool) -> dict[str, int]:
        """Index documents using async backend for high throughput."""
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
                        logger.warning(f"Failed to prepare {fp} for indexing: {e}")
            return docs

        documents = await asyncio.to_thread(_prepare_documents_sync)
        if not documents:
            return {}

        assert self._async_search is not None
        return await self._async_search.index_documents_bulk(documents)
