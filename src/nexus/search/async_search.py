"""Async semantic search implementation for high-throughput scenarios.

Provides fully async semantic search using:
- asyncpg (PostgreSQL) or aiosqlite (SQLite) for non-blocking DB operations
- Parallel embedding generation with batching
- Optimized bulk insert/query operations
- pg_textsearch BM25 ranking (PostgreSQL 17+) for true relevance ranking

BM25 ranking (pg_textsearch):
- True BM25 with IDF, term frequency saturation, length normalization
- ~10ms queries vs ts_rank's 25-30s degradation at 800K rows
- Falls back to ts_rank() on PostgreSQL < 17 or when extension unavailable

This module is designed for high-load scenarios where blocking DB operations
would impact throughput.

Usage:
    from nexus.search.async_search import AsyncSemanticSearch

    search = AsyncSemanticSearch(database_url, embedding_provider)
    await search.initialize()

    # Index documents (fully async)
    await search.index_documents(["/doc1.md", "/doc2.md"])

    # Search (fully async)
    results = await search.search("authentication", limit=10)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.llm.context_builder import ContextBuilder
from nexus.search.chunking import ChunkStrategy, DocumentChunker, EntropyAwareChunker
from nexus.search.contextual_chunking import (
    ContextGenerator,
    ContextualChunker,
    ContextualChunkingConfig,
    ContextualChunkResult,
)
from nexus.search.embeddings import EmbeddingProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from nexus.search.bm25s_search import BM25SIndex

logger = logging.getLogger(__name__)


@dataclass
class AsyncSearchResult:
    """Async search result with full metadata."""

    path: str
    chunk_index: int
    chunk_text: str
    score: float
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None


def create_async_engine_from_url(database_url: str) -> AsyncEngine:
    """Create async engine from database URL.

    Converts sync URLs to async driver URLs:
    - postgresql:// -> postgresql+asyncpg://
    - sqlite:// -> sqlite+aiosqlite://

    Args:
        database_url: Sync database URL

    Returns:
        Async SQLAlchemy engine
    """
    is_sqlite = False
    if database_url.startswith("postgresql://"):
        async_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif database_url.startswith("sqlite:///"):
        async_url = database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        is_sqlite = True
    elif database_url.startswith("sqlite://"):
        async_url = database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        is_sqlite = True
    else:
        async_url = database_url

    # SQLite with aiosqlite uses NullPool and doesn't support pool parameters
    if is_sqlite:
        return create_async_engine(async_url)

    # PostgreSQL with asyncpg supports full connection pooling
    return create_async_engine(
        async_url,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=30,
        pool_recycle=1800,
    )


class AsyncSemanticSearch:
    """Fully async semantic search for high-throughput scenarios.

    Key optimizations:
    - Non-blocking DB operations (asyncpg/aiosqlite)
    - Batched embedding generation
    - Bulk insert operations
    - Parallel processing pipeline
    """

    def __init__(
        self,
        database_url: str,
        embedding_provider: EmbeddingProvider | None = None,
        chunk_size: int = 1024,
        chunk_strategy: ChunkStrategy = ChunkStrategy.SEMANTIC,
        batch_size: int = 100,
        entropy_filtering: bool = False,
        entropy_threshold: float = 0.35,
        entropy_alpha: float = 0.5,
        contextual_chunking: bool = False,
        contextual_config: ContextualChunkingConfig | None = None,
        context_generator: ContextGenerator | None = None,
    ):
        """Initialize async semantic search.

        Args:
            database_url: Database URL (postgresql:// or sqlite://)
            embedding_provider: Embedding provider (optional for keyword-only)
            chunk_size: Chunk size in tokens
            chunk_strategy: Chunking strategy
            batch_size: Batch size for bulk operations
            entropy_filtering: Enable entropy-aware filtering (Issue #1024)
            entropy_threshold: Redundancy threshold for entropy filtering (default: 0.35)
            entropy_alpha: Balance between entity and semantic novelty (default: 0.5)
            contextual_chunking: Enable contextual chunking (Issue #1192)
            contextual_config: Configuration for contextual chunking
            context_generator: Callable that generates context for each chunk
        """
        self.database_url = database_url
        self.embedding_provider = embedding_provider
        self.batch_size = batch_size
        self.entropy_filtering = entropy_filtering
        self.entropy_threshold = entropy_threshold
        self.entropy_alpha = entropy_alpha

        # Create async engine and session factory
        self.engine = create_async_engine_from_url(database_url)
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Chunker
        self.chunker = DocumentChunker(
            chunk_size=chunk_size,
            strategy=chunk_strategy,
        )

        # Entropy-aware chunker (Issue #1024)
        self._entropy_chunker: EntropyAwareChunker | None = None
        if entropy_filtering:
            self._entropy_chunker = EntropyAwareChunker(
                redundancy_threshold=entropy_threshold,
                alpha=entropy_alpha,
                embedding_provider=embedding_provider,
                base_chunker=self.chunker,
            )

        # Issue #1192: Contextual chunking
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

        # Detect DB type
        self.db_type = "postgresql" if "postgresql" in database_url else "sqlite"

        # Feature flags
        self.bm25_available = False  # Set to True if pg_textsearch BM25 is available

        # BM25S index for fast ranked text search (Issue #796)
        self._bm25s_index: BM25SIndex | None = None
        self._bm25s_enabled = False

    @asynccontextmanager
    async def _session(self) -> Any:
        """Get async database session with cancellation-safe cleanup.

        Uses asyncio.shield() to protect cleanup from task cancellation,
        preventing connection leaks when queries are interrupted by timeouts.
        """
        async with self.async_session() as session:
            try:
                yield session
            finally:
                await asyncio.shield(session.close())

    async def initialize(self) -> None:
        """Initialize database extensions (vector, FTS) and BM25S index."""
        async with self.async_session() as session:
            if self.db_type == "postgresql":
                await self._init_postgresql(session)
            else:
                await self._init_sqlite(session)

        # Initialize BM25S index (Issue #796)
        await self._init_bm25s()

    async def _init_bm25s(self) -> None:
        """Initialize BM25S index for fast ranked text search."""
        try:
            from nexus.search.bm25s_search import BM25SIndex, is_bm25s_available

            if not is_bm25s_available():
                logger.debug("BM25S not available (bm25s package not installed)")
                return

            self._bm25s_index = BM25SIndex()
            if await self._bm25s_index.initialize():
                self._bm25s_enabled = True
                logger.info("BM25S index initialized for fast ranked text search")
            else:
                logger.warning("BM25S index initialization failed")
        except ImportError:
            logger.debug("BM25S not available (bm25s package not installed)")
        except Exception as e:
            logger.warning(f"Could not initialize BM25S: {e}")

    async def _init_postgresql(self, session: AsyncSession) -> None:
        """Initialize pgvector and pg_textsearch extensions."""
        # Initialize pgvector for semantic search
        try:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await session.commit()
            logger.info("pgvector extension initialized")
        except Exception as e:
            logger.warning(f"Could not initialize pgvector: {e}")

        # Initialize pg_textsearch for BM25 ranking (PostgreSQL 17+)
        try:
            result = await session.execute(text("SHOW server_version_num"))
            version_scalar = result.scalar()
            version_num = int(version_scalar) if version_scalar else 0

            if version_num >= 170000:
                await session.execute(text("CREATE EXTENSION IF NOT EXISTS pg_textsearch"))
                await session.commit()
                # Verify the extension is loaded
                result = await session.execute(
                    text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1")
                )
                if result.scalar():
                    self.bm25_available = True
                    logger.info("pg_textsearch BM25 extension initialized")
        except Exception as e:
            logger.debug(f"pg_textsearch not available: {e}. Using ts_rank fallback.")

    async def _init_sqlite(self, session: AsyncSession) -> None:
        """Initialize sqlite-vec and FTS5."""
        # FTS5 table for keyword search
        try:
            await session.execute(
                text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
                USING fts5(chunk_id, chunk_text, content='document_chunks', content_rowid='rowid')
            """)
            )
            await session.commit()
            logger.info("FTS5 table initialized")
        except Exception as e:
            logger.warning(f"Could not initialize FTS5: {e}")

    async def index_document(
        self,
        path: str,
        content: str,
        path_id: str,
    ) -> int:
        """Index a single document asynchronously.

        Args:
            path: Virtual path of the document
            content: Document content
            path_id: Path ID from file_paths table

        Returns:
            Number of chunks indexed
        """
        # Issue #1192: Contextual chunking takes priority (includes base chunking internally)
        contextual_result: ContextualChunkResult | None = None
        if self.contextual_chunking and self._contextual_chunker is not None:
            doc_summary = content[:500].rsplit(". ", 1)[0] + "." if ". " in content[:500] else content[:500]
            contextual_result = await self._contextual_chunker.chunk_with_context(
                document=content,
                doc_summary=doc_summary,
                file_path=path,
                compute_lines=True,
            )
            chunks = [cc.chunk for cc in contextual_result.chunks]
        elif self.entropy_filtering and self._entropy_chunker:
            # Issue #1024: Entropy-aware chunking to filter redundant content
            entropy_result = await self._entropy_chunker.chunk_with_filtering(
                content, path, compute_lines=True
            )
            chunks = entropy_result.chunks
            if entropy_result.original_count > 0:
                logger.debug(
                    f"[ASYNC-SEARCH] Entropy filtering for {path}: "
                    f"{entropy_result.original_count} -> {entropy_result.filtered_count} chunks "
                    f"({entropy_result.reduction_percent:.1f}% reduction)"
                )
        else:
            # Regular chunking (CPU-bound, run in executor)
            loop = asyncio.get_event_loop()
            chunks = await loop.run_in_executor(None, lambda: self.chunker.chunk(content, path))

        if not chunks:
            return 0

        # Generate embeddings (async API call)
        # When contextual chunking is active, embed composed text (context + original)
        embeddings = None
        if self.embedding_provider:
            if contextual_result is not None:
                chunk_texts = [cc.contextual_text for cc in contextual_result.chunks]
            else:
                chunk_texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding_provider.embed_texts_batched(
                chunk_texts,
                batch_size=self.batch_size,
                parallel=True,
            )

        # Bulk insert chunks
        await self._bulk_insert_chunks(path_id, chunks, embeddings, contextual_result)

        # Index in BM25S for fast ranked text search (Issue #796)
        if self._bm25s_enabled and self._bm25s_index:
            try:
                await self._bm25s_index.index_document(path_id, path, content)
            except Exception as e:
                logger.debug(f"BM25S indexing failed for {path}: {e}")

        return len(chunks)

    async def index_documents_bulk(
        self,
        documents: list[tuple[str, str, str]],  # (path, content, path_id)
    ) -> dict[str, int]:
        """Index multiple documents in parallel.

        Args:
            documents: List of (path, content, path_id) tuples

        Returns:
            Dict mapping path to number of chunks indexed
        """
        results: dict[str, int] = {}

        # Process in batches to avoid overwhelming the system
        batch_size = 10  # Process 10 documents concurrently

        for i in range(0, len(documents), batch_size):
            batch = documents[i : i + batch_size]

            # Create tasks for parallel processing
            tasks = [
                self.index_document(path, content, path_id) for path, content, path_id in batch
            ]

            # Wait for all tasks in batch
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect results
            for (path, _, _), result in zip(batch, batch_results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(f"Failed to index {path}: {result}")
                    results[path] = 0
                else:
                    results[path] = int(result)

        return results

    async def _bulk_insert_chunks(
        self,
        path_id: str,
        chunks: list[Any],
        embeddings: list[list[float]] | None,
        contextual_result: ContextualChunkResult | None = None,
    ) -> None:
        """Bulk insert chunks with embeddings.

        Args:
            path_id: Path ID
            chunks: Document chunks
            embeddings: Optional embeddings
            contextual_result: Optional contextual chunking result (Issue #1192)
        """
        async with self.async_session() as session:
            # Delete existing chunks first
            await session.execute(
                text("DELETE FROM document_chunks WHERE path_id = :path_id"),
                {"path_id": path_id},
            )

            # Prepare bulk insert values
            now = datetime.now(UTC)
            embedding_model = (
                str(self.embedding_provider.__class__.__name__) if self.embedding_provider else None
            )
            source_document_id = (
                contextual_result.source_document_id if contextual_result else None
            )

            # Issue #1192: Pre-compute contextual metadata outside the insert loop
            context_jsons: list[str | None] = []
            context_positions: list[int | None] = []
            if contextual_result is not None:
                for cc in contextual_result.chunks:
                    context_positions.append(cc.position)
                    context_jsons.append(
                        cc.context.model_dump_json() if cc.context is not None else None
                    )

            for i, chunk in enumerate(chunks):
                chunk_id = str(uuid.uuid4())

                chunk_context_json = context_jsons[i] if context_jsons else None
                chunk_position = context_positions[i] if context_positions else None

                # Insert chunk
                if self.db_type == "postgresql" and embeddings:
                    # PostgreSQL with pgvector
                    await session.execute(
                        text("""
                            INSERT INTO document_chunks
                            (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens,
                             start_offset, end_offset, line_start, line_end,
                             embedding_model, embedding,
                             chunk_context, chunk_position, source_document_id,
                             created_at)
                            VALUES
                            (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens,
                             :start_offset, :end_offset, :line_start, :line_end,
                             :embedding_model, :embedding::halfvec,
                             :chunk_context, :chunk_position, :source_document_id,
                             :created_at)
                        """),
                        {
                            "chunk_id": chunk_id,
                            "path_id": path_id,
                            "chunk_index": i,
                            "chunk_text": chunk.text,
                            "chunk_tokens": chunk.tokens,
                            "start_offset": chunk.start_offset,
                            "end_offset": chunk.end_offset,
                            "line_start": chunk.line_start,
                            "line_end": chunk.line_end,
                            "embedding_model": embedding_model,
                            "embedding": embeddings[i] if embeddings else None,
                            "chunk_context": chunk_context_json,
                            "chunk_position": chunk_position,
                            "source_document_id": source_document_id,
                            "created_at": now,
                        },
                    )
                else:
                    # SQLite or no embeddings
                    await session.execute(
                        text("""
                            INSERT INTO document_chunks
                            (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens,
                             start_offset, end_offset, line_start, line_end,
                             embedding_model,
                             chunk_context, chunk_position, source_document_id,
                             created_at)
                            VALUES
                            (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens,
                             :start_offset, :end_offset, :line_start, :line_end,
                             :embedding_model,
                             :chunk_context, :chunk_position, :source_document_id,
                             :created_at)
                        """),
                        {
                            "chunk_id": chunk_id,
                            "path_id": path_id,
                            "chunk_index": i,
                            "chunk_text": chunk.text,
                            "chunk_tokens": chunk.tokens,
                            "start_offset": chunk.start_offset,
                            "end_offset": chunk.end_offset,
                            "line_start": chunk.line_start,
                            "line_end": chunk.line_end,
                            "embedding_model": embedding_model,
                            "chunk_context": chunk_context_json,
                            "chunk_position": chunk_position,
                            "source_document_id": source_document_id,
                            "created_at": now,
                        },
                    )

            await session.commit()

    async def search(
        self,
        query: str,
        limit: int = 10,
        path_filter: str | None = None,
        search_mode: str = "hybrid",
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        rrf_k: int = 60,
        adaptive_k: bool = False,
    ) -> list[AsyncSearchResult]:
        """Search documents asynchronously.

        Args:
            query: Search query
            limit: Maximum results (used as k_base when adaptive_k=True)
            path_filter: Optional path prefix filter
            search_mode: "keyword", "semantic", or "hybrid"
            alpha: Weight for vector search in hybrid mode (0.0 = all BM25, 1.0 = all vector)
            fusion_method: Fusion method for hybrid: "rrf" (default), "weighted", "rrf_weighted"
            rrf_k: RRF constant (default: 60)
            adaptive_k: If True, dynamically adjust limit based on query complexity (Issue #1021)

        Returns:
            List of search results
        """
        # Apply adaptive k if enabled (Issue #1021)
        if adaptive_k:
            context_builder = ContextBuilder()
            original_limit = limit
            limit = context_builder.calculate_k_dynamic(query, k_base=limit)
            if limit != original_limit:
                logger.info(
                    "[ASYNC-SEARCH] Adaptive k applied: %d -> %d for query: %s",
                    original_limit,
                    limit,
                    query[:50],
                )

        async with self.async_session() as session:
            if search_mode == "keyword":
                results = await self._keyword_search(session, query, limit, path_filter)
            elif search_mode == "semantic":
                if not self.embedding_provider:
                    raise ValueError("Semantic search requires embedding provider")
                query_embedding = await self.embedding_provider.embed_text(query)
                results = await self._vector_search(session, query_embedding, limit, path_filter)
            else:  # hybrid
                results = await self._hybrid_search(
                    session,
                    query,
                    limit,
                    path_filter,
                    alpha=alpha,
                    fusion_method=fusion_method,
                    rrf_k=rrf_k,
                )

        return results

    async def _keyword_search(
        self,
        session: AsyncSession,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[AsyncSearchResult]:
        """Async keyword search using Zoekt, BM25S, database BM25, or FTS fallback.

        Search priority:
        1. Zoekt (fast trigram-based code search)
        2. BM25S (fast in-memory BM25 with code-aware tokenization, Issue #796)
        3. pg_textsearch BM25 (PostgreSQL 17+ with true relevance ranking)
        4. ts_rank (PostgreSQL fallback)
        5. FTS5 (SQLite)
        """
        # Try Zoekt first for accelerated search
        zoekt_results = await self._try_keyword_search_with_zoekt(query, limit, path_filter)
        if zoekt_results is not None:
            logger.debug(f"[KEYWORD-ASYNC] Zoekt returned {len(zoekt_results)} results")
            return zoekt_results

        # Try BM25S for fast ranked text search (Issue #796)
        bm25s_results = await self._try_keyword_search_with_bm25s(query, limit, path_filter)
        if bm25s_results is not None:
            logger.debug(f"[KEYWORD-ASYNC] BM25S returned {len(bm25s_results)} results")
            return bm25s_results

        # Fall back to database FTS
        logger.debug("[KEYWORD-ASYNC] Using database FTS fallback")
        if self.db_type == "postgresql":
            if self.bm25_available:
                # Use pg_textsearch BM25 ranking (PostgreSQL 17+)
                # BM25 scores are negative (lower = better), ORDER BY ASC
                sql = text("""
                    SELECT
                        c.chunk_id, c.chunk_index, c.chunk_text,
                        c.start_offset, c.end_offset, c.line_start, c.line_end,
                        fp.virtual_path,
                        c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25') as score
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE (:path_filter IS NULL OR fp.virtual_path LIKE :path_pattern)
                    ORDER BY c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25')
                    LIMIT :limit
                """)
                logger.debug("[KEYWORD-ASYNC] Using pg_textsearch BM25 ranking")
            else:
                # Fall back to ts_rank (slower at scale)
                sql = text("""
                    SELECT
                        c.chunk_id, c.chunk_index, c.chunk_text,
                        c.start_offset, c.end_offset, c.line_start, c.line_end,
                        fp.virtual_path,
                        ts_rank(to_tsvector('english', c.chunk_text),
                                plainto_tsquery('english', :query)) as score
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
                      AND (:path_filter IS NULL OR fp.virtual_path LIKE :path_pattern)
                    ORDER BY score DESC
                    LIMIT :limit
                """)
                logger.debug("[KEYWORD-ASYNC] Using ts_rank fallback")
        else:
            # SQLite FTS5
            sql = text("""
                SELECT
                    c.chunk_id, c.chunk_index, c.chunk_text,
                    c.start_offset, c.end_offset, c.line_start, c.line_end,
                    fp.virtual_path,
                    fts.rank as score
                FROM document_chunks_fts fts
                JOIN document_chunks c ON c.chunk_id = fts.chunk_id
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE fts.chunk_text MATCH :query
                  AND (:path_filter IS NULL OR fp.virtual_path LIKE :path_pattern)
                ORDER BY fts.rank
                LIMIT :limit
            """)

        result = await session.execute(
            sql,
            {
                "query": query,
                "limit": limit,
                "path_filter": path_filter,
                "path_pattern": f"{path_filter}%" if path_filter else None,
            },
        )

        return [
            AsyncSearchResult(
                path=row.virtual_path,
                chunk_index=row.chunk_index,
                chunk_text=row.chunk_text,
                score=abs(float(row.score)),  # BM25 and FTS5 scores are negative
                start_offset=row.start_offset,
                end_offset=row.end_offset,
                line_start=row.line_start,
                line_end=row.line_end,
                keyword_score=abs(float(row.score)),
            )
            for row in result
        ]

    async def _try_keyword_search_with_zoekt(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[AsyncSearchResult] | None:
        """Try to use Zoekt for async keyword search.

        Args:
            query: Search query
            limit: Maximum results
            path_filter: Optional path prefix

        Returns:
            List of results if Zoekt succeeded, None to fall back to FTS
        """
        try:
            from nexus.search.zoekt_client import get_zoekt_client
        except ImportError:
            return None

        client = get_zoekt_client()

        if not await client.is_available():
            return None

        logger.debug("[KEYWORD-ASYNC] Using Zoekt for accelerated search")

        try:
            # Build Zoekt query
            zoekt_query = query
            if path_filter:
                zoekt_query = f"file:{path_filter.lstrip('/')} {zoekt_query}"

            matches = await client.search(zoekt_query, num=limit * 2)

            if not matches:
                return None

            # Convert Zoekt results to AsyncSearchResult format
            results = []
            for match in matches[:limit]:
                results.append(
                    AsyncSearchResult(
                        path=match.file,
                        chunk_index=0,
                        chunk_text=match.content,
                        score=match.score or 1.0,
                        start_offset=0,
                        end_offset=len(match.content),
                        line_start=match.line,
                        line_end=match.line,
                        keyword_score=match.score or 1.0,
                    )
                )

            logger.debug(f"[KEYWORD-ASYNC] Zoekt: {len(matches)} matches, returning {len(results)}")
            return results

        except Exception as e:
            logger.warning(f"[KEYWORD-ASYNC] Zoekt search failed: {e}")
            return None

    async def _try_keyword_search_with_bm25s(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[AsyncSearchResult] | None:
        """Try to use BM25S for async keyword search (Issue #796).

        BM25S provides fast ranked text search with:
        - Code-aware tokenization (camelCase, snake_case splitting)
        - In-memory sparse matrix scoring (500x faster than rank-bm25)
        - True BM25 with IDF weighting

        Args:
            query: Search query
            limit: Maximum results
            path_filter: Optional path prefix

        Returns:
            List of results if BM25S succeeded, None to fall back to database FTS
        """
        if not self._bm25s_enabled or self._bm25s_index is None:
            return None

        logger.debug("[KEYWORD-ASYNC] Using BM25S for fast ranked text search")

        try:
            bm25s_results = await self._bm25s_index.search(
                query=query,
                limit=limit,
                path_filter=path_filter,
            )

            if not bm25s_results:
                # No results from BM25S, fall back to database FTS
                return None

            # Convert BM25S results to AsyncSearchResult format
            results = []
            for r in bm25s_results:
                results.append(
                    AsyncSearchResult(
                        path=r.path,
                        chunk_index=0,
                        chunk_text=r.content_preview,
                        score=r.score,
                        start_offset=0,
                        end_offset=len(r.content_preview),
                        line_start=1,
                        line_end=None,
                        keyword_score=r.score,
                    )
                )

            logger.debug(f"[KEYWORD-ASYNC] BM25S: {len(results)} results")
            return results

        except Exception as e:
            logger.warning(f"[KEYWORD-ASYNC] BM25S search failed: {e}")
            return None

    async def _vector_search(
        self,
        session: AsyncSession,
        embedding: list[float],
        limit: int,
        path_filter: str | None,
    ) -> list[AsyncSearchResult]:
        """Async vector search using pgvector."""
        if self.db_type != "postgresql":
            raise ValueError("Vector search requires PostgreSQL with pgvector")

        sql = text("""
            SELECT
                c.chunk_id, c.chunk_index, c.chunk_text,
                c.start_offset, c.end_offset, c.line_start, c.line_end,
                fp.virtual_path,
                1 - (c.embedding <=> CAST(:embedding AS halfvec)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL
              AND (:path_filter IS NULL OR fp.virtual_path LIKE :path_pattern)
            ORDER BY c.embedding <=> CAST(:embedding AS halfvec)
            LIMIT :limit
        """)

        result = await session.execute(
            sql,
            {
                "embedding": embedding,
                "limit": limit,
                "path_filter": path_filter,
                "path_pattern": f"{path_filter}%" if path_filter else None,
            },
        )

        return [
            AsyncSearchResult(
                path=row.virtual_path,
                chunk_index=row.chunk_index,
                chunk_text=row.chunk_text,
                score=float(row.score),
                start_offset=row.start_offset,
                end_offset=row.end_offset,
                line_start=row.line_start,
                line_end=row.line_end,
                vector_score=float(row.score),
            )
            for row in result
        ]

    async def _hybrid_search(
        self,
        session: AsyncSession,
        query: str,
        limit: int,
        path_filter: str | None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        rrf_k: int = 60,
    ) -> list[AsyncSearchResult]:
        """Async hybrid search using configurable fusion method.

        Combines keyword and vector search results using the specified fusion algorithm.

        Args:
            session: Database session
            query: Search query
            limit: Maximum results
            path_filter: Path filter
            alpha: Weight for vector search (0.0 = all BM25, 1.0 = all vector)
            fusion_method: "rrf" (default), "weighted", or "rrf_weighted"
            rrf_k: RRF constant (default: 60)

        Returns:
            Combined search results
        """
        from nexus.search.fusion import FusionConfig, FusionMethod, fuse_results

        # Run keyword and vector search in parallel
        keyword_task = self._keyword_search(session, query, limit * 3, path_filter)

        vector_task = None
        if self.embedding_provider:
            query_embedding = await self.embedding_provider.embed_text(query)
            vector_task = self._vector_search(session, query_embedding, limit * 3, path_filter)

        # Wait for results
        keyword_results = await keyword_task
        vector_results = await vector_task if vector_task else []

        # Convert AsyncSearchResult to dict for fusion
        keyword_dicts = [
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
            for r in keyword_results
        ]

        vector_dicts = [
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
            for r in vector_results
        ]

        # Create fusion config
        config = FusionConfig(
            method=FusionMethod(fusion_method),
            alpha=alpha,
            rrf_k=rrf_k,
        )

        # Use shared fusion logic
        fused = fuse_results(
            keyword_dicts,
            vector_dicts,
            config=config,
            limit=limit,
            id_key=None,  # Use path:chunk_index as key
        )

        # Convert back to AsyncSearchResult
        return [
            AsyncSearchResult(
                path=r["path"],
                chunk_index=r["chunk_index"],
                chunk_text=r["chunk_text"],
                score=r["score"],
                start_offset=r.get("start_offset"),
                end_offset=r.get("end_offset"),
                line_start=r.get("line_start"),
                line_end=r.get("line_end"),
                keyword_score=r.get("keyword_score"),
                vector_score=r.get("vector_score"),
            )
            for r in fused
        ]

    async def delete_document(self, path_id: str) -> None:
        """Delete document index."""
        async with self.async_session() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE path_id = :path_id"),
                {"path_id": path_id},
            )
            await session.commit()

        # Delete from BM25S index (Issue #796)
        if self._bm25s_enabled and self._bm25s_index:
            try:
                await self._bm25s_index.delete_document(path_id)
            except Exception as e:
                logger.debug(f"BM25S delete failed for {path_id}: {e}")

    async def get_stats(self) -> dict[str, Any]:
        """Get index statistics."""
        async with self.async_session() as session:
            result = await session.execute(
                text("""
                    SELECT
                        COUNT(*) as total_chunks,
                        COUNT(DISTINCT path_id) as indexed_files
                    FROM document_chunks
                """)
            )
            row = result.one()

            stats = {
                "total_chunks": row.total_chunks,
                "indexed_files": row.indexed_files,
                "db_type": self.db_type,
                "embedding_provider": (
                    str(self.embedding_provider.__class__.__name__)
                    if self.embedding_provider
                    else None
                ),
            }

        # Add BM25S stats (Issue #796)
        if self._bm25s_enabled and self._bm25s_index:
            try:
                bm25s_stats = await self._bm25s_index.get_stats()
                stats["bm25s"] = bm25s_stats
            except Exception as e:
                logger.debug(f"Failed to get BM25S stats: {e}")

        return stats

    async def close(self) -> None:
        """Close database connections."""
        await self.engine.dispose()
