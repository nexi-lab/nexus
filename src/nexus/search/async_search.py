"""Async semantic search implementation for high-throughput scenarios.

Provides fully async semantic search using:
- asyncpg (PostgreSQL) or aiosqlite (SQLite) for non-blocking DB operations
- Parallel embedding generation with batching
- Optimized bulk insert/query operations

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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.search.chunking import ChunkStrategy, DocumentChunker
from nexus.search.embeddings import EmbeddingProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

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
    if database_url.startswith("postgresql://"):
        async_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif database_url.startswith("sqlite:///"):
        async_url = database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    elif database_url.startswith("sqlite://"):
        async_url = database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    else:
        async_url = database_url

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
    ):
        """Initialize async semantic search.

        Args:
            database_url: Database URL (postgresql:// or sqlite://)
            embedding_provider: Embedding provider (optional for keyword-only)
            chunk_size: Chunk size in tokens
            chunk_strategy: Chunking strategy
            batch_size: Batch size for bulk operations
        """
        self.database_url = database_url
        self.embedding_provider = embedding_provider
        self.batch_size = batch_size

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

        # Detect DB type
        self.db_type = "postgresql" if "postgresql" in database_url else "sqlite"

    async def initialize(self) -> None:
        """Initialize database extensions (vector, FTS)."""
        async with self.async_session() as session:
            if self.db_type == "postgresql":
                await self._init_postgresql(session)
            else:
                await self._init_sqlite(session)

    async def _init_postgresql(self, session: AsyncSession) -> None:
        """Initialize pgvector extension."""
        try:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await session.commit()
            logger.info("pgvector extension initialized")
        except Exception as e:
            logger.warning(f"Could not initialize pgvector: {e}")

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
        # Chunk document (CPU-bound, run in executor)
        loop = asyncio.get_event_loop()
        chunks = await loop.run_in_executor(None, lambda: self.chunker.chunk(content, path))

        if not chunks:
            return 0

        # Generate embeddings (async API call)
        embeddings = None
        if self.embedding_provider:
            chunk_texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding_provider.embed_texts_batched(
                chunk_texts,
                batch_size=self.batch_size,
                parallel=True,
            )

        # Bulk insert chunks
        await self._bulk_insert_chunks(path_id, chunks, embeddings)

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
    ) -> None:
        """Bulk insert chunks with embeddings.

        Args:
            path_id: Path ID
            chunks: Document chunks
            embeddings: Optional embeddings
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

            for i, chunk in enumerate(chunks):
                chunk_id = str(uuid.uuid4())

                # Insert chunk
                if self.db_type == "postgresql" and embeddings:
                    # PostgreSQL with pgvector
                    await session.execute(
                        text("""
                            INSERT INTO document_chunks
                            (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens,
                             start_offset, end_offset, line_start, line_end,
                             embedding_model, embedding, created_at)
                            VALUES
                            (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens,
                             :start_offset, :end_offset, :line_start, :line_end,
                             :embedding_model, :embedding::vector, :created_at)
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
                             embedding_model, created_at)
                            VALUES
                            (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens,
                             :start_offset, :end_offset, :line_start, :line_end,
                             :embedding_model, :created_at)
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
    ) -> list[AsyncSearchResult]:
        """Search documents asynchronously.

        Args:
            query: Search query
            limit: Maximum results
            path_filter: Optional path prefix filter
            search_mode: "keyword", "semantic", or "hybrid"

        Returns:
            List of search results
        """
        async with self.async_session() as session:
            if search_mode == "keyword":
                results = await self._keyword_search(session, query, limit, path_filter)
            elif search_mode == "semantic":
                if not self.embedding_provider:
                    raise ValueError("Semantic search requires embedding provider")
                query_embedding = await self.embedding_provider.embed_text(query)
                results = await self._vector_search(session, query_embedding, limit, path_filter)
            else:  # hybrid
                results = await self._hybrid_search(session, query, limit, path_filter)

        return results

    async def _keyword_search(
        self,
        session: AsyncSession,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[AsyncSearchResult]:
        """Async keyword search using FTS."""
        if self.db_type == "postgresql":
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
        else:
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
                score=abs(float(row.score)),
                start_offset=row.start_offset,
                end_offset=row.end_offset,
                line_start=row.line_start,
                line_end=row.line_end,
                keyword_score=abs(float(row.score)),
            )
            for row in result
        ]

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
                1 - (c.embedding <=> CAST(:embedding AS vector)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL
              AND (:path_filter IS NULL OR fp.virtual_path LIKE :path_pattern)
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
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
        k: int = 60,
    ) -> list[AsyncSearchResult]:
        """Async hybrid search using RRF (Reciprocal Rank Fusion).

        Combines keyword and vector search results using RRF algorithm.

        Args:
            session: Database session
            query: Search query
            limit: Maximum results
            path_filter: Path filter
            k: RRF constant (default: 60)

        Returns:
            Combined search results
        """
        # Run keyword and vector search in parallel
        keyword_task = self._keyword_search(session, query, limit * 3, path_filter)

        vector_task = None
        if self.embedding_provider:
            query_embedding = await self.embedding_provider.embed_text(query)
            vector_task = self._vector_search(session, query_embedding, limit * 3, path_filter)

        # Wait for results
        keyword_results = await keyword_task
        vector_results = await vector_task if vector_task else []

        # RRF fusion
        rrf_scores: dict[str, dict[str, Any]] = {}

        # Add keyword results
        for rank, result in enumerate(keyword_results, start=1):
            key = f"{result.path}:{result.chunk_index}"
            if key not in rrf_scores:
                rrf_scores[key] = {"result": result, "score": 0.0}
            rrf_scores[key]["score"] += 1.0 / (k + rank)
            rrf_scores[key]["result"].keyword_score = result.score

        # Add vector results
        for rank, result in enumerate(vector_results, start=1):
            key = f"{result.path}:{result.chunk_index}"
            if key not in rrf_scores:
                rrf_scores[key] = {"result": result, "score": 0.0}
            rrf_scores[key]["score"] += 1.0 / (k + rank)
            rrf_scores[key]["result"].vector_score = result.score

        # Sort by RRF score
        sorted_results = sorted(
            rrf_scores.values(),
            key=lambda x: x["score"],
            reverse=True,
        )[:limit]

        # Update final scores
        for item in sorted_results:
            item["result"].score = item["score"]

        return [item["result"] for item in sorted_results]

    async def delete_document(self, path_id: str) -> None:
        """Delete document index."""
        async with self.async_session() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE path_id = :path_id"),
                {"path_id": path_id},
            )
            await session.commit()

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

            return {
                "total_chunks": row.total_chunks,
                "indexed_files": row.indexed_files,
                "db_type": self.db_type,
                "embedding_provider": (
                    str(self.embedding_provider.__class__.__name__)
                    if self.embedding_provider
                    else None
                ),
            }

    async def close(self) -> None:
        """Close database connections."""
        await self.engine.dispose()
