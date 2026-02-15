"""Semantic search implementation for Nexus.

Provides semantic search capabilities using vector embeddings with:
- SQLite: sqlite-vec for vectors, FTS5 for keywords
- PostgreSQL: pgvector for vectors, pg_textsearch BM25 for keywords (PG17+)

BM25 ranking (pg_textsearch):
- True BM25 with IDF, term frequency saturation, length normalization
- ~10ms queries vs ts_rank's 25-30s degradation at 800K rows
- Falls back to ts_rank() on PostgreSQL < 17 or when extension unavailable

Supports hybrid search combining keyword (FTS/BM25) and semantic (vector) search.

Issue #1021: Supports adaptive retrieval depth based on query complexity.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from nexus.search.chunking import (
    ChunkStrategy,
    DocumentChunker,
    EntropyAwareChunker,
    EntropyFilterResult,
)
from nexus.search.contextual_chunking import (
    ContextGenerator,
    ContextualChunker,
    ContextualChunkingConfig,
)
from nexus.search.embeddings import EmbeddingProvider
from nexus.search.results import BaseSearchResult
from nexus.search.vector_db import VectorDatabase
from nexus.storage.models import DocumentChunkModel, FilePathModel

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.llm.context_builder import AdaptiveRetrievalConfig
    from nexus.search.ranking import RankingConfig


@dataclass
class SemanticSearchResult(BaseSearchResult):
    """A semantic search result with source location metadata.

    Extends BaseSearchResult with attribute ranking fields (Issue #1092).
    """

    # Issue #1092: Attribute ranking metadata
    matched_field: str | None = None  # Which field matched (filename, path, content, etc.)
    attribute_boost: float | None = None  # Boost multiplier applied
    original_score: float | None = None  # Score before attribute boosting


class SemanticSearch:
    """Semantic search engine for Nexus.

    Provides semantic and hybrid search using database-native extensions:
    - SQLite: sqlite-vec for vectors, FTS5 for keywords
    - PostgreSQL: pgvector for vectors, pg_textsearch BM25 for keywords (PG17+)

    Falls back to tsvector/ts_rank on PostgreSQL < 17 or when pg_textsearch unavailable.
    """

    def __init__(
        self,
        nx: NexusFS,
        embedding_provider: EmbeddingProvider | None = None,
        chunk_size: int = 1024,
        chunk_strategy: ChunkStrategy = ChunkStrategy.SEMANTIC,
        adaptive_config: AdaptiveRetrievalConfig | None = None,
        entropy_filtering: bool = False,
        entropy_threshold: float = 0.35,
        entropy_alpha: float = 0.5,
        ranking_config: RankingConfig | None = None,
        engine: Any | None = None,
        contextual_chunking: bool = False,
        contextual_config: ContextualChunkingConfig | None = None,
        context_generator: ContextGenerator | None = None,
    ):
        """Initialize semantic search.

        Args:
            nx: NexusFS instance
            embedding_provider: Embedding provider (optional - needed for semantic/hybrid search)
            chunk_size: Chunk size in tokens
            chunk_strategy: Chunking strategy
            adaptive_config: Configuration for adaptive retrieval depth (Issue #1021)
            entropy_filtering: If True, filter redundant chunks before indexing (Issue #1024)
            entropy_threshold: Redundancy threshold for entropy filtering (default: 0.35)
                               Chunks scoring below this are filtered out
            entropy_alpha: Balance between entity novelty (α) and semantic novelty (1-α)
                           Default 0.5 gives equal weight to both signals
            ranking_config: Configuration for attribute-based ranking (Issue #1092)
            engine: SQLAlchemy engine for vector DB (DI from caller; required)
            contextual_chunking: Enable contextual chunking (Issue #1192)
            contextual_config: Configuration for contextual chunking
            context_generator: Callable that generates context for each chunk
        """
        self.nx = nx
        self.chunk_size = chunk_size
        self.chunk_strategy = chunk_strategy

        # Initialize vector database — engine injected by caller (service, not kernel)
        if engine is None:
            raise RuntimeError("SemanticSearch requires a SQL engine (RecordStore)")
        self.vector_db = VectorDatabase(engine)

        # Initialize embedding provider (optional)
        # If None, only keyword search will be available
        self.embedding_provider: EmbeddingProvider | None = embedding_provider

        # Initialize chunker
        self.chunker = DocumentChunker(
            chunk_size=chunk_size, strategy=chunk_strategy, overlap_size=128
        )

        # Adaptive retrieval config (Issue #1021)
        # ContextBuilder injected lazily on first use (Issue #1520: DI over hard import)
        self.adaptive_config = adaptive_config
        self._context_builder: Any = None

        # Initialize entropy-aware chunker (Issue #1024)
        self.entropy_filtering = entropy_filtering
        self.entropy_threshold = entropy_threshold
        self.entropy_alpha = entropy_alpha
        self._entropy_chunker: EntropyAwareChunker | None = None
        if entropy_filtering:
            self._entropy_chunker = EntropyAwareChunker(
                redundancy_threshold=entropy_threshold,
                alpha=entropy_alpha,
                embedding_provider=embedding_provider,
                base_chunker=self.chunker,
            )

        # Issue #1192: Initialize contextual chunking
        self.contextual_chunking = contextual_chunking
        self._contextual_config = contextual_config or ContextualChunkingConfig(
            enabled=contextual_chunking
        )
        self._contextual_chunker: ContextualChunker | None = None
        if contextual_chunking and context_generator is not None:
            self._contextual_chunker = ContextualChunker(
                context_generator=context_generator,
                config=self._contextual_config,
                base_chunker=self.chunker,
            )

        # Issue #1092: Initialize attribute ranking configuration
        from nexus.search.ranking import RankingConfig

        self.ranking_config = ranking_config or RankingConfig()

    def initialize(self) -> None:
        """Initialize the search engine (create vector extensions and FTS tables)."""
        self.vector_db.initialize()

    async def index_document(self, path: str, force: bool = False) -> int:
        """Index a document for semantic search.

        Uses cached/parsed text when available:
        - For connector files (GCS, S3, etc.): Uses content_cache.content_text
        - For local files: Uses file_metadata.parsed_text
        - Falls back to raw file content if no cached text available

        Implements incremental embedding updates (Issue #865):
        - Skips re-indexing if content_hash matches indexed_content_hash
        - Only re-embeds when file content actually changed
        - Use force=True to bypass the check and re-index anyway

        Args:
            path: Path to the document
            force: If True, re-index even if content hasn't changed

        Returns:
            Number of chunks indexed (0 if skipped due to no changes)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
        """
        # Get path_id and check if re-indexing is needed (Issue #865)
        with self.nx.SessionLocal() as session:
            stmt = select(FilePathModel).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            result = session.execute(stmt)
            file_model = result.scalar_one_or_none()

            if not file_model:
                raise ValueError(f"File not found in database: {path}")

            path_id = file_model.path_id
            current_content_hash = file_model.content_hash

            # Skip re-indexing if content hasn't changed (Issue #865)
            # This avoids expensive embedding API calls for unchanged files
            if (
                not force
                and current_content_hash is not None
                and file_model.indexed_content_hash == current_content_hash
            ):
                # Content unchanged - no need to re-embed
                # Return existing chunk count (projection pushdown - only count, skip embedding)
                chunk_count_stmt = (
                    select(func.count())
                    .select_from(DocumentChunkModel)
                    .where(DocumentChunkModel.path_id == path_id)
                )
                existing_count = session.execute(chunk_count_stmt).scalar() or 0
                return existing_count

        # Try to get searchable text from cache first (content_cache or file_metadata)
        content = self.nx.metadata.get_searchable_text(path)

        # Fall back to reading raw content if no cached text
        if content is None:
            content_raw = self.nx.read(path)
            if isinstance(content_raw, bytes):
                content = content_raw.decode("utf-8", errors="ignore")
            else:
                content = str(content_raw)  # Handle dict or other types

        # Delete existing chunks for this file (bulk DELETE - skip object hydration)
        with self.nx.SessionLocal() as session:
            session.execute(delete(DocumentChunkModel).where(DocumentChunkModel.path_id == path_id))
            session.commit()

        # Issue #1192: Contextual chunking takes priority (includes base chunking internally)
        contextual_result = None
        source_document_id: str | None = None
        entropy_result: EntropyFilterResult | None = None

        if self.contextual_chunking and self._contextual_chunker is not None:
            doc_summary = (
                content[:500].rsplit(". ", 1)[0] + "." if ". " in content[:500] else content[:500]
            )
            contextual_result = await self._contextual_chunker.chunk_with_context(
                document=content,
                doc_summary=doc_summary,
                file_path=path,
                compute_lines=True,
            )
            source_document_id = contextual_result.source_document_id
            chunks = [cc.chunk for cc in contextual_result.chunks]
        elif self.entropy_filtering and self._entropy_chunker:
            # Issue #1024: Entropy-aware chunking to filter redundant chunks
            entropy_result = await self._entropy_chunker.chunk_with_filtering(
                content, path, compute_lines=True
            )
            chunks = entropy_result.chunks
            logger.info(
                "[SEMANTIC-SEARCH] Entropy filtering for %s: %d -> %d chunks (%.1f%% reduction)",
                path,
                entropy_result.original_count,
                entropy_result.filtered_count,
                entropy_result.reduction_percent,
            )
        else:
            chunks = self.chunker.chunk(content, path)

        if not chunks:
            # Update tracking even for empty files (Issue #865)
            with self.nx.SessionLocal() as session:
                file_model = session.get(FilePathModel, path_id)
                if file_model:
                    file_model.indexed_content_hash = current_content_hash
                    file_model.last_indexed_at = datetime.now(UTC)
                    session.commit()
            return 0

        # Generate embeddings (if provider available)
        # When contextual chunking is active, embed the composed text (context + original)
        embeddings = None
        if self.embedding_provider:
            if contextual_result is not None:
                chunk_texts = [cc.contextual_text for cc in contextual_result.chunks]
            else:
                chunk_texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding_provider.embed_texts(chunk_texts)

        # Issue #1192: Pre-compute contextual metadata outside the insert loop
        context_jsons: list[str | None] = []
        context_positions: list[int | None] = []
        if contextual_result is not None:
            for cc in contextual_result.chunks:
                context_positions.append(cc.position)
                context_jsons.append(
                    cc.context.model_dump_json() if cc.context is not None else None
                )

        # Store chunks in database with optional embeddings
        with self.nx.SessionLocal() as session:
            chunk_ids = []
            for i, chunk in enumerate(chunks):
                chunk_id = str(uuid.uuid4())
                chunk_ids.append(chunk_id)

                chunk_model = DocumentChunkModel(
                    chunk_id=chunk_id,
                    path_id=path_id,
                    chunk_index=i,
                    chunk_text=chunk.text,
                    chunk_tokens=chunk.tokens,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    embedding_model=str(self.embedding_provider.__class__.__name__)
                    if self.embedding_provider
                    else None,
                    chunk_context=context_jsons[i] if context_jsons else None,
                    chunk_position=context_positions[i] if context_positions else None,
                    source_document_id=source_document_id,
                    created_at=datetime.now(UTC),
                )
                session.add(chunk_model)

            session.commit()

            # Store embeddings if available AND vector extension is available
            if embeddings and self.vector_db.vec_available:
                for chunk_id, embedding in zip(chunk_ids, embeddings, strict=False):
                    self.vector_db.store_embedding(session, chunk_id, embedding)
                session.commit()

            # Update indexing tracking fields (Issue #865)
            file_model = session.get(FilePathModel, path_id)
            if file_model:
                file_model.indexed_content_hash = current_content_hash
                file_model.last_indexed_at = datetime.now(UTC)
                session.commit()

        return len(chunks)

    async def index_directory(self, path: str = "/") -> dict[str, int]:
        """Index all documents in a directory.

        Args:
            path: Root path to index (default: all files)

        Returns:
            Dictionary mapping file paths to number of chunks indexed
        """
        # List all files
        files_result = self.nx.list(path, recursive=True)

        # Handle PaginatedResult if returned
        files = files_result.items if hasattr(files_result, "items") else files_result

        # Filter to indexable files (exclude binary files, etc.)
        indexable_files = []
        for file in files:
            file_path = file if isinstance(file, str) else file.get("name", "")
            # Skip directories and non-text files
            if not file_path or file_path.endswith("/"):
                continue
            # Skip common binary extensions
            if file_path.endswith(
                (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".pdf",
                    ".zip",
                    ".tar",
                    ".gz",
                    ".exe",
                    ".bin",
                )
            ):
                continue
            indexable_files.append(file_path)

        # Index each file
        results = {}
        for file_path in indexable_files:
            try:
                num_chunks = await self.index_document(file_path)
                results[file_path] = num_chunks
            except Exception as e:
                # Log error but continue
                import warnings

                warnings.warn(f"Failed to index {file_path}: {e}", stacklevel=2)
                results[file_path] = -1  # Indicate error

        return results

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,  # noqa: ARG002
        search_mode: str = "semantic",
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        adaptive_k: bool = False,
    ) -> list[SemanticSearchResult]:
        """Search documents.

        Args:
            query: Natural language query
            path: Root path to search (default: all files)
            limit: Maximum number of results (used as k_base when adaptive_k=True)
            filters: Optional filters (currently unused)
            search_mode: Search mode - "semantic", "keyword", or "hybrid" (default: "semantic")
            alpha: Weight for vector search in hybrid mode (0.0 = all BM25, 1.0 = all vector)
            fusion_method: Fusion method for hybrid: "rrf" (default), "weighted", "rrf_weighted"
            adaptive_k: If True, dynamically adjust limit based on query complexity (Issue #1021)

        Returns:
            List of search results ranked by relevance

        Example:
            >>> # Hybrid search favoring keyword matches
            >>> results = await search.search(
            ...     "authentication handler",
            ...     search_mode="hybrid",
            ...     alpha=0.3,  # Favor BM25
            ...     fusion_method="rrf",
            ... )

            >>> # Adaptive retrieval - adjusts k based on query complexity
            >>> results = await search.search(
            ...     "How does authentication compare to authorization?",
            ...     adaptive_k=True,  # Will increase limit for complex query
            ... )
        """
        # Apply adaptive k if enabled (Issue #1021)
        original_limit = limit
        if adaptive_k and self.adaptive_config is not None and self.adaptive_config.enabled:
            # Lazy import to break search → llm hard dependency (Issue #1520)
            if self._context_builder is None:
                from nexus.llm.context_builder import (
                    ContextBuilder as _CB,
                )

                self._context_builder = _CB(adaptive_config=self.adaptive_config)
            limit = self._context_builder.calculate_k_dynamic(query, k_base=limit)
            if limit != original_limit:
                logger.info(
                    "[SEMANTIC-SEARCH] Adaptive k applied: %d -> %d for query: %s",
                    original_limit,
                    limit,
                    query[:50],
                )

        # Build path filter
        path_filter = path if path != "/" else None

        with self.nx.SessionLocal() as session:
            if search_mode == "keyword":
                # Keyword-only search using FTS (no embeddings needed)
                results = self.vector_db.keyword_search(
                    session, query, limit=limit, path_filter=path_filter
                )
            elif search_mode == "hybrid":
                # Hybrid search (keyword + semantic) - requires embedding provider AND vector extension
                if not self.embedding_provider:
                    raise ValueError(
                        "Hybrid search requires an embedding provider. "
                        "Install with: pip install nexus-ai-fs[semantic-search-remote]"
                    )
                if not self.vector_db.vec_available:
                    raise ValueError(
                        "Hybrid search requires vector database extension. "
                        "Install sqlite-vec (https://github.com/asg017/sqlite-vec) "
                        "or pgvector (https://github.com/pgvector/pgvector). "
                        "Use search_mode='keyword' for FTS-only search."
                    )

                # Run embedding generation and keyword search in parallel
                # This provides ~2x speedup for hybrid search
                embedding_task = self.embedding_provider.embed_text(query)
                keyword_task = asyncio.to_thread(
                    self.vector_db.keyword_search,
                    session,
                    query,
                    limit * 3,  # Over-fetch for better fusion
                    path_filter,
                )

                query_embedding, keyword_results = await asyncio.gather(
                    embedding_task, keyword_task, return_exceptions=True
                )

                # Handle exceptions
                if isinstance(query_embedding, BaseException):
                    raise query_embedding
                if isinstance(keyword_results, BaseException):
                    logger.warning(f"Keyword search failed: {keyword_results}")
                    keyword_results = []

                # Run vector search with the embedding (sequential - needs embedding)
                vector_results = self.vector_db.vector_search(
                    session, query_embedding, limit * 3, path_filter
                )

                # Fuse results using shared algorithm
                from nexus.search.fusion import FusionConfig, FusionMethod, fuse_results

                config = FusionConfig(
                    method=FusionMethod(fusion_method),
                    alpha=alpha,
                    rrf_k=60,
                )
                results = fuse_results(
                    keyword_results,
                    vector_results,
                    config=config,
                    limit=limit,
                    id_key="chunk_id",
                )
            else:
                # Semantic-only search (default) - requires embedding provider AND vector extension
                if not self.embedding_provider:
                    raise ValueError(
                        "Semantic search requires an embedding provider. "
                        "Install with: pip install nexus-ai-fs[semantic-search-remote] "
                        "Or use search_mode='keyword' for FTS-only search (no embeddings needed)"
                    )
                if not self.vector_db.vec_available:
                    raise ValueError(
                        "Semantic search requires vector database extension. "
                        "Install sqlite-vec (https://github.com/asg017/sqlite-vec) "
                        "or pgvector (https://github.com/pgvector/pgvector). "
                        "Use search_mode='keyword' for FTS-only search."
                    )
                query_embedding = await self.embedding_provider.embed_text(query)
                results = self.vector_db.vector_search(
                    session, query_embedding, limit=limit, path_filter=path_filter
                )

        # Issue #1092: Apply attribute boosting
        if self.ranking_config.enable_attribute_boosting:
            from nexus.search.ranking import apply_attribute_boosting

            results = apply_attribute_boosting(results, query, self.ranking_config)

        # Convert to SemanticSearchResult
        search_results = []
        for result in results:
            search_results.append(
                SemanticSearchResult(
                    path=result["path"],
                    chunk_index=result["chunk_index"],
                    chunk_text=result["chunk_text"],
                    score=result["score"],
                    start_offset=result.get("start_offset"),
                    end_offset=result.get("end_offset"),
                    line_start=result.get("line_start"),
                    line_end=result.get("line_end"),
                    keyword_score=result.get("keyword_score"),
                    vector_score=result.get("vector_score"),
                    # Issue #1092: Attribute ranking metadata
                    matched_field=result.get("matched_field"),
                    attribute_boost=result.get("attribute_boost"),
                    original_score=result.get("original_score"),
                )
            )

        return search_results

    async def delete_document_index(self, path: str) -> None:
        """Delete document index.

        Args:
            path: Path to the document
        """
        # Get path_id from database
        with self.nx.SessionLocal() as session:
            stmt = select(FilePathModel).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            result = session.execute(stmt)
            file_model = result.scalar_one_or_none()

            if not file_model:
                return  # File not found, nothing to delete

            path_id = file_model.path_id

            # Delete chunks from database (embeddings are in the same table)
            del_stmt = select(DocumentChunkModel).where(DocumentChunkModel.path_id == path_id)
            del_result = session.execute(del_stmt)
            chunks = del_result.scalars().all()

            for chunk in chunks:
                session.delete(chunk)

            session.commit()

    async def get_index_stats(self) -> dict[str, Any]:
        """Get indexing statistics.

        Returns:
            Dictionary with statistics
        """
        # Count total chunks (projection pushdown - COUNT() instead of loading 3KB embeddings)
        # For 100K chunks, this saves ~300-600MB of memory/transfer
        with self.nx.SessionLocal() as session:
            total_chunks = (
                session.execute(select(func.count()).select_from(DocumentChunkModel)).scalar() or 0
            )

            # Count indexed files (distinct path_ids)
            indexed_files = (
                session.execute(
                    select(func.count(func.distinct(DocumentChunkModel.path_id)))
                ).scalar()
                or 0
            )

        has_embeddings = self.embedding_provider is not None

        return {
            "total_chunks": total_chunks,
            "indexed_files": indexed_files,
            "embedding_model": str(self.embedding_provider.__class__.__name__)
            if self.embedding_provider
            else None,
            "chunk_size": self.chunk_size,
            "chunk_strategy": self.chunk_strategy.value,
            "database_type": self.vector_db.db_type,
            # Issue #1024: Entropy filtering configuration
            "entropy_filtering": {
                "enabled": self.entropy_filtering,
                "threshold": self.entropy_threshold,
                "alpha": self.entropy_alpha,
            },
            "search_capabilities": {
                "semantic": has_embeddings,
                "keyword": True,  # Always available via FTS
                "hybrid": has_embeddings,
            },
        }

    # Backward compatibility wrapper methods
    async def keyword_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        adaptive_k: bool = False,
    ) -> list[SemanticSearchResult]:
        """Keyword search (wrapper for search with mode='keyword')."""
        return await self.search(
            query, path=path, limit=limit, search_mode="keyword", adaptive_k=adaptive_k
        )

    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        adaptive_k: bool = False,
    ) -> list[SemanticSearchResult]:
        """Semantic search (wrapper for search with mode='semantic')."""
        return await self.search(
            query, path=path, limit=limit, search_mode="semantic", adaptive_k=adaptive_k
        )

    async def hybrid_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        adaptive_k: bool = False,
    ) -> list[SemanticSearchResult]:
        """Hybrid search combining keyword (BM25) and semantic (vector) search.

        Args:
            query: Search query
            path: Root path to search (default: all files)
            limit: Maximum results (used as k_base when adaptive_k=True)
            alpha: Weight for vector search (0.0 = all BM25, 1.0 = all vector)
            fusion_method: "rrf" (default), "weighted", "rrf_weighted"
            adaptive_k: If True, dynamically adjust limit based on query complexity

        Returns:
            List of search results ranked by fusion score
        """
        return await self.search(
            query,
            path=path,
            limit=limit,
            search_mode="hybrid",
            alpha=alpha,
            fusion_method=fusion_method,
            adaptive_k=adaptive_k,
        )

    async def get_stats(self) -> dict[str, Any]:
        """Get stats (wrapper for get_index_stats)."""
        return await self.get_index_stats()

    async def delete_document(self, path: str) -> None:
        """Delete document (wrapper for delete_document_index)."""
        return await self.delete_document_index(path)

    async def clear_index(self) -> None:
        """Clear the entire search index."""
        with self.nx.SessionLocal() as session:
            # Delete all chunks
            session.query(DocumentChunkModel).delete()
            session.commit()

    async def shutdown(self) -> None:
        """Shutdown search engine (SearchBrickProtocol)."""
        self.close()

    def close(self) -> None:
        """Close the search engine (no-op for now)."""
        pass

    def verify_imports(self) -> dict[str, bool]:
        """Verify required and optional imports (SearchBrickProtocol)."""
        import importlib

        results: dict[str, bool] = {}
        for mod in [
            "nexus.search.fusion",
            "nexus.search.chunking",
            "nexus.search.embeddings",
            "nexus.search.results",
        ]:
            try:
                importlib.import_module(mod)
                results[mod] = True
            except ImportError:
                results[mod] = False
        for mod in ["nexus.search.bm25s_search", "nexus.search.zoekt_client"]:
            try:
                importlib.import_module(mod)
                results[mod] = True
            except ImportError:
                results[mod] = False
        return results
