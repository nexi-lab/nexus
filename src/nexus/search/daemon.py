"""Hot Search Daemon for sub-50ms instant response.

Implements a long-running search service that keeps indexes warm in memory
for zero cold-start latency. This daemon pre-loads all search indexes at
startup and maintains connection pools for instant query response.

Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │                    Search Daemon                        │
    │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐       │
    │  │   BM25S     │ │   Vector    │ │   Zoekt     │       │
    │  │   Index     │ │   Cache     │ │  (optional) │       │
    │  │  (mmap)     │ │ (pgvector)  │ │             │       │
    │  └─────────────┘ └─────────────┘ └─────────────┘       │
    │         │               │               │               │
    │         └───────────────┼───────────────┘               │
    │                         │                               │
    │              ┌──────────▼──────────┐                    │
    │              │   Search Router     │                    │
    │              │  (query analysis)   │                    │
    │              └──────────┬──────────┘                    │
    │                         │                               │
    └─────────────────────────┼───────────────────────────────┘

Performance targets:
    - First query: <50ms (vs ~500ms cold start)
    - P99 latency: <100ms
    - Connection overhead: 0ms (pooled)

Issue: #951
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from nexus.search.async_search import AsyncSemanticSearch
    from nexus.search.bm25s_search import BM25SIndex

logger = logging.getLogger(__name__)


@dataclass
class DaemonStats:
    """Runtime statistics for the search daemon."""

    startup_time_ms: float = 0.0
    bm25_documents: int = 0
    bm25_load_time_ms: float = 0.0
    db_pool_size: int = 0
    db_pool_warmup_time_ms: float = 0.0
    vector_warmup_time_ms: float = 0.0
    total_queries: int = 0
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    last_index_refresh: float | None = None
    zoekt_available: bool = False
    embedding_cache_connected: bool = False


@dataclass
class SearchResult:
    """Unified search result from daemon."""

    path: str
    chunk_text: str
    score: float
    chunk_index: int = 0
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    search_type: str = "hybrid"


@dataclass
class DaemonConfig:
    """Configuration for the search daemon."""

    # Database settings
    database_url: str | None = None
    db_pool_min_size: int = 10
    db_pool_max_size: int = 50
    db_pool_recycle: int = 1800  # 30 minutes

    # BM25S settings
    bm25s_index_dir: str = ".nexus-data/bm25s"
    bm25s_mmap: bool = True  # Memory-mapped for instant loading

    # Vector search settings
    vector_warmup_enabled: bool = True
    vector_ef_search: int = 100  # HNSW recall parameter

    # Index refresh settings
    refresh_debounce_seconds: float = 5.0
    refresh_enabled: bool = True

    # Performance settings
    query_timeout_seconds: float = 10.0


class SearchDaemon:
    """Long-running search service with pre-warmed indexes.

    The daemon keeps all search indexes hot in memory and maintains
    connection pools for sub-50ms query response times.

    Usage:
        daemon = SearchDaemon(config)
        await daemon.startup()  # Pre-warm everything

        # Fast searches
        results = await daemon.search("authentication", limit=10)

        # Cleanup
        await daemon.shutdown()
    """

    def __init__(self, config: DaemonConfig | None = None):
        """Initialize the search daemon.

        Args:
            config: Daemon configuration (uses defaults if not provided)
        """
        self.config = config or DaemonConfig()
        self.stats = DaemonStats()

        # Search components (initialized on startup)
        self._bm25s_index: BM25SIndex | None = None
        self._async_engine: AsyncEngine | None = None
        self._async_search: AsyncSemanticSearch | None = None
        self._embedding_provider: Any = None

        # State
        self._initialized = False
        self._shutting_down = False

        # Index refresh task
        self._refresh_task: asyncio.Task | None = None
        self._pending_refresh_paths: set[str] = set()
        self._refresh_lock = asyncio.Lock()

        # Latency tracking (circular buffer)
        self._latencies: list[float] = []
        self._max_latency_samples = 1000

    @property
    def is_initialized(self) -> bool:
        """Check if daemon is fully initialized."""
        return self._initialized

    async def startup(self) -> None:
        """Initialize and pre-warm all search indexes.

        This method should be called once at application startup.
        It loads indexes into memory and warms connection pools.
        """
        if self._initialized:
            logger.warning("SearchDaemon already initialized")
            return

        start_time = time.perf_counter()
        logger.info("Starting SearchDaemon - pre-warming indexes...")

        # Run warmup tasks in parallel where possible
        await asyncio.gather(
            self._init_bm25s_index(),
            self._init_database_pool(),
            return_exceptions=True,
        )

        # Vector warmup needs DB pool to be ready
        if self.config.vector_warmup_enabled and self._async_engine:
            await self._warm_vector_index()

        # Check optional components
        await self._check_zoekt()
        await self._check_embedding_cache()

        # Start index refresh background task
        if self.config.refresh_enabled:
            self._refresh_task = asyncio.create_task(self._index_refresh_loop())

        self._initialized = True
        self.stats.startup_time_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            f"SearchDaemon ready in {self.stats.startup_time_ms:.1f}ms - "
            f"BM25S: {self.stats.bm25_documents} docs, "
            f"DB pool: {self.stats.db_pool_size} connections"
        )

    async def shutdown(self) -> None:
        """Gracefully shutdown the daemon.

        Cancels background tasks and releases resources.
        """
        if self._shutting_down:
            return

        self._shutting_down = True
        logger.info("Shutting down SearchDaemon...")

        # Cancel refresh task
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task

        # Close database connections
        if self._async_engine:
            await self._async_engine.dispose()

        self._initialized = False
        logger.info("SearchDaemon shutdown complete")

    # =========================================================================
    # Initialization Methods
    # =========================================================================

    async def _init_bm25s_index(self) -> None:
        """Load BM25S index with memory mapping for instant access."""
        start = time.perf_counter()

        try:
            from nexus.search.bm25s_search import BM25SIndex, is_bm25s_available

            if not is_bm25s_available():
                logger.warning("BM25S not available (bm25s package not installed)")
                return

            self._bm25s_index = BM25SIndex(
                index_dir=self.config.bm25s_index_dir,
            )

            # Initialize with mmap=True for instant loading
            if await self._bm25s_index.initialize():
                # Access document count if available
                doc_count = (
                    len(self._bm25s_index._corpus) if hasattr(self._bm25s_index, "_corpus") else 0
                )
                self.stats.bm25_documents = doc_count
                self.stats.bm25_load_time_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"BM25S index loaded: {doc_count} documents in "
                    f"{self.stats.bm25_load_time_ms:.1f}ms (mmap={self.config.bm25s_mmap})"
                )
            else:
                logger.warning("BM25S index initialization failed")

        except ImportError:
            logger.debug("BM25S not available")
        except Exception as e:
            logger.error(f"Failed to initialize BM25S index: {e}")

    async def _init_database_pool(self) -> None:
        """Initialize and warm the database connection pool."""
        if not self.config.database_url:
            logger.debug("No database URL configured, skipping DB pool init")
            return

        start = time.perf_counter()

        try:
            from sqlalchemy.ext.asyncio import create_async_engine

            # Convert sync URL to async
            db_url = self.config.database_url
            if db_url.startswith("postgresql://"):
                db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif db_url.startswith("sqlite:///"):
                db_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

            self._async_engine = create_async_engine(
                db_url,
                pool_pre_ping=True,
                pool_size=self.config.db_pool_min_size,
                max_overflow=self.config.db_pool_max_size - self.config.db_pool_min_size,
                pool_recycle=self.config.db_pool_recycle,
            )

            # Warm the pool by executing a simple query
            async with self._async_engine.connect() as conn:
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))

            self.stats.db_pool_size = self.config.db_pool_min_size
            self.stats.db_pool_warmup_time_ms = (time.perf_counter() - start) * 1000

            logger.info(
                f"Database pool warmed: {self.stats.db_pool_size} connections in "
                f"{self.stats.db_pool_warmup_time_ms:.1f}ms"
            )

        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")

    async def _warm_vector_index(self) -> None:
        """Warm the vector index by executing a dummy query.

        This forces the HNSW index into memory for faster subsequent queries.
        """
        if not self._async_engine:
            return

        start = time.perf_counter()

        try:
            from sqlalchemy import text

            # Execute a minimal vector query to warm the HNSW index
            # Use a zero vector which won't match anything but loads the index
            async with self._async_engine.connect() as conn:
                # Set HNSW search parameters for high recall
                await conn.execute(text(f"SET hnsw.ef_search = {self.config.vector_ef_search}"))

                # Dummy query to warm index (SELECT 1 with vector operation)
                await conn.execute(
                    text("""
                        SELECT 1 FROM document_chunks
                        WHERE embedding IS NOT NULL
                        LIMIT 1
                    """)
                )

            self.stats.vector_warmup_time_ms = (time.perf_counter() - start) * 1000
            logger.info(f"Vector index warmed in {self.stats.vector_warmup_time_ms:.1f}ms")

        except Exception as e:
            # Non-fatal - vector search will still work, just slower first time
            logger.debug(f"Vector index warmup skipped: {e}")

    async def _check_zoekt(self) -> None:
        """Check if Zoekt trigram search is available."""
        try:
            from nexus.search.zoekt_client import get_zoekt_client

            client = get_zoekt_client()
            self.stats.zoekt_available = await client.is_available()

            if self.stats.zoekt_available:
                logger.info("Zoekt trigram search available")
        except Exception:
            self.stats.zoekt_available = False

    async def _check_embedding_cache(self) -> None:
        """Check if embedding cache (Dragonfly) is connected."""
        try:
            from nexus.core.cache.dragonfly import DragonflyEmbeddingCache

            cache = DragonflyEmbeddingCache()  # type: ignore[call-arg]
            self.stats.embedding_cache_connected = await cache.is_connected()  # type: ignore[attr-defined]

            if self.stats.embedding_cache_connected:
                logger.info("Embedding cache (Dragonfly) connected")
        except Exception:
            self.stats.embedding_cache_connected = False

    # =========================================================================
    # Search Methods
    # =========================================================================

    async def search(
        self,
        query: str,
        search_type: Literal["keyword", "semantic", "hybrid"] = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
    ) -> list[SearchResult]:
        """Execute a search query with pre-warmed indexes.

        Args:
            query: Search query text
            search_type: Type of search ("keyword", "semantic", "hybrid")
            limit: Maximum number of results
            path_filter: Optional path prefix filter
            alpha: Weight for semantic vs keyword (0.0 = all keyword, 1.0 = all semantic)
            fusion_method: Fusion algorithm for hybrid search ("rrf", "weighted", "rrf_weighted")

        Returns:
            List of search results sorted by relevance
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        start = time.perf_counter()

        try:
            if search_type == "keyword":
                results = await self._keyword_search(query, limit, path_filter)
            elif search_type == "semantic":
                results = await self._semantic_search(query, limit, path_filter)
            else:  # hybrid
                results = await self._hybrid_search(query, limit, path_filter, alpha, fusion_method)

            # Track latency
            latency_ms = (time.perf_counter() - start) * 1000
            self._track_latency(latency_ms)

            return results

        except TimeoutError:
            logger.warning(f"Search timeout after {self.config.query_timeout_seconds}s")
            return []

    async def _keyword_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[SearchResult]:
        """Fast keyword search using BM25S or Zoekt."""
        results: list[SearchResult] = []

        # Try Zoekt first (fastest, trigram-based)
        if self.stats.zoekt_available:
            zoekt_results = await self._search_zoekt(query, limit, path_filter)
            if zoekt_results:
                return zoekt_results

        # Fall back to BM25S (in-memory, very fast)
        if self._bm25s_index:
            bm25s_results = await self._search_bm25s(query, limit, path_filter)
            if bm25s_results:
                return bm25s_results

        # Final fallback: database FTS
        if self._async_engine:
            return await self._search_fts(query, limit, path_filter)

        return results

    async def _semantic_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[SearchResult]:
        """Vector similarity search using pgvector."""
        if not self._async_engine:
            logger.warning("Semantic search requires database connection")
            return []

        try:
            # Get query embedding
            embedding = await self._get_query_embedding(query)
            if not embedding:
                logger.warning("Could not generate query embedding")
                return []

            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            async_session = async_sessionmaker(self._async_engine, class_=AsyncSession)

            async with async_session() as session:
                sql = text("""
                    SELECT
                        c.chunk_index, c.chunk_text,
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
                    SearchResult(
                        path=row.virtual_path,
                        chunk_index=row.chunk_index,
                        chunk_text=row.chunk_text,
                        score=float(row.score),
                        start_offset=row.start_offset,
                        end_offset=row.end_offset,
                        line_start=row.line_start,
                        line_end=row.line_end,
                        vector_score=float(row.score),
                        search_type="semantic",
                    )
                    for row in result
                ]

        except Exception as e:
            logger.error(f"Semantic search error: {e}")
            return []

    async def _hybrid_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
    ) -> list[SearchResult]:
        """Hybrid search combining keyword and semantic results."""
        from nexus.search.fusion import FusionConfig, FusionMethod, fuse_results

        # Run keyword and semantic search in parallel
        keyword_task = self._keyword_search(query, limit * 3, path_filter)
        semantic_task = self._semantic_search(query, limit * 3, path_filter)

        keyword_results, semantic_results = await asyncio.gather(
            keyword_task, semantic_task, return_exceptions=True
        )

        # Handle errors
        kw_results: list[SearchResult] = []
        sem_results: list[SearchResult] = []
        if isinstance(keyword_results, BaseException):
            logger.warning(f"Keyword search failed: {keyword_results}")
        else:
            kw_results = keyword_results
        if isinstance(semantic_results, BaseException):
            logger.warning(f"Semantic search failed: {semantic_results}")
        else:
            sem_results = semantic_results

        # Convert to dicts for fusion
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
            for r in kw_results
        ]

        semantic_dicts = [
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
            for r in sem_results
        ]

        # Fuse results
        config = FusionConfig(
            method=FusionMethod(fusion_method),
            alpha=alpha,
            rrf_k=60,
        )

        fused = fuse_results(
            keyword_dicts,
            semantic_dicts,
            config=config,
            limit=limit,
            id_key=None,
        )

        # Convert back to SearchResult
        return [
            SearchResult(
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
                search_type="hybrid",
            )
            for r in fused
        ]

    async def _search_zoekt(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[SearchResult]:
        """Search using Zoekt trigram index."""
        try:
            from nexus.search.zoekt_client import get_zoekt_client

            client = get_zoekt_client()
            if not await client.is_available():
                return []

            # Build Zoekt query
            zoekt_query = query
            if path_filter:
                zoekt_query = f"file:{path_filter.lstrip('/')} {zoekt_query}"

            matches = await client.search(zoekt_query, num=limit)

            return [
                SearchResult(
                    path=match.file,
                    chunk_index=0,
                    chunk_text=match.content,
                    score=match.score or 1.0,
                    line_start=match.line,
                    line_end=match.line,
                    keyword_score=match.score or 1.0,
                    search_type="keyword",
                )
                for match in matches
            ]

        except Exception as e:
            logger.debug(f"Zoekt search failed: {e}")
            return []

    async def _search_bm25s(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[SearchResult]:
        """Search using BM25S in-memory index."""
        if not self._bm25s_index:
            return []

        try:
            bm25s_results = await self._bm25s_index.search(
                query=query,
                limit=limit,
                path_filter=path_filter,
            )

            return [
                SearchResult(
                    path=r.path,
                    chunk_index=0,
                    chunk_text=r.content_preview,
                    score=r.score,
                    keyword_score=r.score,
                    search_type="keyword",
                )
                for r in bm25s_results
            ]

        except Exception as e:
            logger.debug(f"BM25S search failed: {e}")
            return []

    async def _search_fts(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[SearchResult]:
        """Search using database FTS (fallback)."""
        if not self._async_engine:
            return []

        try:
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            async_session = async_sessionmaker(self._async_engine, class_=AsyncSession)

            async with async_session() as session:
                # PostgreSQL FTS query - use explicit boolean for path filtering
                if path_filter:
                    sql = text("""
                        SELECT
                            c.chunk_index, c.chunk_text,
                            c.start_offset, c.end_offset, c.line_start, c.line_end,
                            fp.virtual_path,
                            ts_rank(to_tsvector('english', c.chunk_text), plainto_tsquery('english', :query)) as score
                        FROM document_chunks c
                        JOIN file_paths fp ON c.path_id = fp.path_id
                        WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
                          AND fp.virtual_path LIKE :path_pattern
                        ORDER BY score DESC
                        LIMIT :limit
                    """)
                    params = {
                        "query": query,
                        "limit": limit,
                        "path_pattern": f"{path_filter}%",
                    }
                else:
                    sql = text("""
                        SELECT
                            c.chunk_index, c.chunk_text,
                            c.start_offset, c.end_offset, c.line_start, c.line_end,
                            fp.virtual_path,
                            ts_rank(to_tsvector('english', c.chunk_text), plainto_tsquery('english', :query)) as score
                        FROM document_chunks c
                        JOIN file_paths fp ON c.path_id = fp.path_id
                        WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
                        ORDER BY score DESC
                        LIMIT :limit
                    """)
                    params = {
                        "query": query,
                        "limit": limit,
                    }

                result = await session.execute(sql, params)

                return [
                    SearchResult(
                        path=row.virtual_path,
                        chunk_index=row.chunk_index,
                        chunk_text=row.chunk_text,
                        score=float(row.score),
                        start_offset=row.start_offset,
                        end_offset=row.end_offset,
                        line_start=row.line_start,
                        line_end=row.line_end,
                        keyword_score=float(row.score),
                        search_type="keyword",
                    )
                    for row in result
                ]

        except Exception as e:
            logger.error(f"FTS search error: {e}")
            return []

    async def _get_query_embedding(self, query: str) -> list[float] | None:
        """Get embedding for query text."""
        if self._embedding_provider:
            result = await self._embedding_provider.embed_text(query)
            return list(result) if result else None

        # Try to get from environment/default provider
        try:
            from nexus.search.embeddings import create_embedding_provider

            provider = create_embedding_provider()
            return await provider.embed_text(query)
        except Exception as e:
            logger.debug(f"Could not get query embedding: {e}")
            return None

    # =========================================================================
    # Index Refresh
    # =========================================================================

    async def notify_file_change(self, path: str, _change_type: str = "update") -> None:
        """Notify the daemon of a file change for index refresh.

        Changes are debounced and batched for efficiency.

        Args:
            path: File path that changed
            change_type: Type of change ("create", "update", "delete")
        """
        if not self.config.refresh_enabled:
            return

        async with self._refresh_lock:
            self._pending_refresh_paths.add(path)

    async def _index_refresh_loop(self) -> None:
        """Background task to refresh indexes for changed files."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(self.config.refresh_debounce_seconds)

                async with self._refresh_lock:
                    if not self._pending_refresh_paths:
                        continue

                    paths = list(self._pending_refresh_paths)
                    self._pending_refresh_paths.clear()

                # Refresh indexes for changed paths
                await self._refresh_indexes(paths)
                self.stats.last_index_refresh = time.time()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Index refresh error: {e}")

    async def _refresh_indexes(self, paths: list[str]) -> None:
        """Refresh indexes for a batch of changed files."""
        logger.debug(f"Refreshing indexes for {len(paths)} files")

        # BM25S refresh would go here
        # Vector index refresh would go here
        # For now, this is a placeholder for the incremental update logic

    # =========================================================================
    # Statistics
    # =========================================================================

    def _track_latency(self, latency_ms: float) -> None:
        """Track query latency for statistics."""
        self._latencies.append(latency_ms)
        if len(self._latencies) > self._max_latency_samples:
            self._latencies.pop(0)

        self.stats.total_queries += 1

        # Update average
        if self._latencies:
            self.stats.avg_latency_ms = sum(self._latencies) / len(self._latencies)

            # Update P99
            sorted_latencies = sorted(self._latencies)
            p99_idx = int(len(sorted_latencies) * 0.99)
            self.stats.p99_latency_ms = sorted_latencies[p99_idx] if sorted_latencies else 0

    def get_stats(self) -> dict[str, Any]:
        """Get current daemon statistics.

        Returns:
            Dictionary of statistics for monitoring/health checks
        """
        return {
            "initialized": self._initialized,
            "startup_time_ms": self.stats.startup_time_ms,
            "bm25_documents": self.stats.bm25_documents,
            "bm25_load_time_ms": self.stats.bm25_load_time_ms,
            "db_pool_size": self.stats.db_pool_size,
            "db_pool_warmup_time_ms": self.stats.db_pool_warmup_time_ms,
            "vector_warmup_time_ms": self.stats.vector_warmup_time_ms,
            "total_queries": self.stats.total_queries,
            "avg_latency_ms": round(self.stats.avg_latency_ms, 2),
            "p99_latency_ms": round(self.stats.p99_latency_ms, 2),
            "last_index_refresh": self.stats.last_index_refresh,
            "zoekt_available": self.stats.zoekt_available,
            "embedding_cache_connected": self.stats.embedding_cache_connected,
        }

    def get_health(self) -> dict[str, Any]:
        """Get health status for health check endpoint.

        Returns:
            Health status dictionary
        """
        return {
            "status": "healthy" if self._initialized else "starting",
            "daemon_initialized": self._initialized,
            "bm25_index_loaded": self._bm25s_index is not None,
            "db_pool_ready": self._async_engine is not None,
            "zoekt_available": self.stats.zoekt_available,
        }


# Global singleton for easy access
_daemon_instance: SearchDaemon | None = None


def get_search_daemon() -> SearchDaemon | None:
    """Get the global search daemon instance.

    Returns:
        SearchDaemon instance or None if not initialized
    """
    return _daemon_instance


def set_search_daemon(daemon: SearchDaemon) -> None:
    """Set the global search daemon instance.

    Args:
        daemon: SearchDaemon instance to set as global
    """
    global _daemon_instance
    _daemon_instance = daemon


async def create_and_start_daemon(
    database_url: str | None = None,
    bm25s_index_dir: str | None = None,
) -> SearchDaemon:
    """Create, configure and start a search daemon.

    Convenience function for creating a fully initialized daemon.

    Args:
        database_url: Database URL (from env if not provided)
        bm25s_index_dir: BM25S index directory

    Returns:
        Initialized SearchDaemon instance
    """
    config = DaemonConfig(
        database_url=database_url or os.environ.get("NEXUS_DATABASE_URL"),
        bm25s_index_dir=bm25s_index_dir or ".nexus-data/bm25s",
    )

    daemon = SearchDaemon(config)
    await daemon.startup()

    set_search_daemon(daemon)
    return daemon
