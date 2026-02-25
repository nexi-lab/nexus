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

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from nexus.bricks.search.results import BaseSearchResult
from nexus.lib.env import get_database_url

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from nexus.bricks.search.bm25s_search import BM25SIndex
    from nexus.bricks.search.chunking import EntropyAwareChunker
    from nexus.bricks.search.indexing import IndexingPipeline

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
class SearchResult(BaseSearchResult):
    """Unified search result from daemon.

    Extends BaseSearchResult with search_type field (Issue #1520).
    """

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
    max_indexing_concurrency: int = 10  # Issue #2071: from ProfileTuning.search

    # Entropy-aware filtering (Issue #1024)
    entropy_filtering: bool = False
    entropy_threshold: float = 0.35  # SimpleMem's τ_redundant
    entropy_alpha: float = 0.5  # Balance entity vs semantic novelty


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

    def __init__(
        self,
        config: DaemonConfig | None = None,
        *,
        async_session_factory: Any | None = None,
        zoekt_client: Any | None = None,
    ):
        """Initialize the search daemon.

        Args:
            config: Daemon configuration (uses defaults if not provided)
            async_session_factory: Injected async_sessionmaker from RecordStoreABC.
                When provided, skips creating a private engine (Issue #1597).
            zoekt_client: Injected ZoektClient instance (Issue #2188).
        """
        self.config = config or DaemonConfig()
        self.stats = DaemonStats()
        self._zoekt_client = zoekt_client

        # Search components (initialized on startup)
        self._bm25s_index: BM25SIndex | None = None
        self._async_engine: AsyncEngine | None = None
        self._async_session: Any | None = None  # async_sessionmaker (Issue #1597)
        self._async_search: Any | None = None  # AsyncSemanticSearch (legacy)
        self._embedding_provider: Any = None
        self._record_store: Any | None = None  # SQLAlchemyRecordStore fallback
        self._owns_engine = False  # True only when we created the engine ourselves

        # Accept injected session factory from RecordStoreABC
        if async_session_factory is not None:
            self._async_session = async_session_factory

        # Entropy-aware chunker for filtering redundant content (Issue #1024)
        self._entropy_chunker: EntropyAwareChunker | None = None

        # Indexing pipeline for parallel refresh (Issue #1094)
        self._indexing_pipeline: IndexingPipeline | None = None

        # State
        self._initialized = False
        self._shutting_down = False

        # Index refresh task
        self._refresh_task: asyncio.Task | None = None
        self._pending_refresh_paths: set[str] = set()
        self._refresh_lock = asyncio.Lock()

        # FileReaderProtocol reference for reading file content (set by FastAPI server)
        # Issue #1520: Replaces direct NexusFS dependency
        self._file_reader: Any = None

        # Issue #2036: Injected adaptive-k provider (replaces lazy import)
        self._adaptive_k_provider: Any = None

        # SPLADE learned sparse retrieval (optional, initialized in startup)
        self._splade: Any = None

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

        # Initialize entropy-aware chunker if enabled (Issue #1024)
        if self.config.entropy_filtering:
            from nexus.bricks.search.chunking import EntropyAwareChunker

            self._entropy_chunker = EntropyAwareChunker(
                redundancy_threshold=self.config.entropy_threshold,
                alpha=self.config.entropy_alpha,
                embedding_provider=self._embedding_provider,
            )
            logger.info(
                f"Entropy filtering enabled: threshold={self.config.entropy_threshold}, "
                f"alpha={self.config.entropy_alpha}"
            )

        # Initialize indexing pipeline for parallel refresh (Issue #1094)
        from nexus.bricks.search.chunking import DocumentChunker
        from nexus.bricks.search.indexing import IndexingPipeline as _IP

        self._indexing_pipeline = _IP(
            chunker=DocumentChunker(),
            embedding_provider=self._embedding_provider,
            entropy_chunker=self._entropy_chunker,
            async_session_factory=self._async_session,
            max_concurrency=self.config.max_indexing_concurrency,
            cross_doc_batching=True,
        )

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

        # Close database connections (only if we created them)
        if self._owns_engine:
            if self._record_store is not None:
                self._record_store.close()
                self._record_store = None
            elif self._async_engine:
                await self._async_engine.dispose()
        self._async_engine = None
        self._async_session = None

        self._initialized = False
        logger.info("SearchDaemon shutdown complete")

    # =========================================================================
    # Initialization Methods
    # =========================================================================

    async def _init_bm25s_index(self) -> None:
        """Load BM25S index with memory mapping for instant access."""
        start = time.perf_counter()

        try:
            from nexus.bricks.search.bm25s_search import BM25SIndex, is_bm25s_available

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
        # If session factory was injected via __init__, skip engine creation
        if self._async_session is not None:
            logger.info("Using injected async_session_factory (RecordStoreABC)")
            return

        if not self.config.database_url:
            logger.debug("No database URL configured, skipping DB pool init")
            return

        start = time.perf_counter()

        try:
            from nexus.storage.record_store import SQLAlchemyRecordStore

            # Delegate engine creation to RecordStoreABC (Issue #615).
            # Pool settings are controlled via NEXUS_DB_POOL_SIZE / NEXUS_DB_MAX_OVERFLOW
            # environment variables inside SQLAlchemyRecordStore.
            self._record_store = SQLAlchemyRecordStore(db_url=self.config.database_url)
            self._async_session = self._record_store.async_session_factory
            self._async_engine = self._record_store._async_engine
            self._owns_engine = True

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
                await conn.execute(
                    text("SELECT set_config('hnsw.ef_search', :val, true)"),
                    {"val": str(self.config.vector_ef_search)},
                )

                # Dummy query to warm index (SELECT 1 with vector operation)
                # Check if embedding column exists first
                # Skip if embedding column doesn't exist yet
                try:
                    await conn.execute(
                        text("""
                            SELECT 1 FROM document_chunks
                            WHERE embedding IS NOT NULL
                            LIMIT 1
                        """)
                    )
                except Exception as e:
                    logger.debug("Vector index warmup query skipped: %s", e)

            self.stats.vector_warmup_time_ms = (time.perf_counter() - start) * 1000
            logger.info(f"Vector index warmed in {self.stats.vector_warmup_time_ms:.1f}ms")

        except Exception as e:
            # Non-fatal - vector search will still work, just slower first time
            logger.debug(f"Vector index warmup skipped: {e}")

    async def _check_zoekt(self) -> None:
        """Check if Zoekt trigram search is available."""
        if self._zoekt_client is None:
            self.stats.zoekt_available = False
            return

        try:
            self.stats.zoekt_available = await self._zoekt_client.is_available()

            if self.stats.zoekt_available:
                logger.info("Zoekt trigram search available")
        except Exception:
            self.stats.zoekt_available = False

    async def _check_embedding_cache(self) -> None:
        """Check if embedding cache (Dragonfly) is connected.

        NOTE: Embedding cache health is now checked via CacheBrick.health_check().
        This method is kept for backward compat but always reports False until
        a CacheBrick reference is wired in (follow-up).
        """
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
        adaptive_k: bool = False,
    ) -> list[SearchResult]:
        """Execute a search query with pre-warmed indexes.

        Args:
            query: Search query text
            search_type: Type of search ("keyword", "semantic", "hybrid")
            limit: Maximum number of results (used as k_base when adaptive_k=True)
            path_filter: Optional path prefix filter
            alpha: Weight for semantic vs keyword (0.0 = all keyword, 1.0 = all semantic)
            fusion_method: Fusion algorithm for hybrid search ("rrf", "weighted", "rrf_weighted")
            adaptive_k: If True, dynamically adjust limit based on query complexity (Issue #1021)

        Returns:
            List of search results sorted by relevance
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        # Apply adaptive k if enabled (Issue #1021, #2036: protocol-based DI)
        if adaptive_k and self._adaptive_k_provider is not None:
            original_limit = limit
            limit = self._adaptive_k_provider.calculate_k_dynamic(query, k_base=limit)
            if limit != original_limit:
                logger.info(
                    "[SEARCH-DAEMON] Adaptive k applied: %d -> %d for query: %s",
                    original_limit,
                    limit,
                    query[:50],
                )

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
        if not self._async_engine or not self._async_session:
            logger.warning("Semantic search requires database connection")
            return []

        try:
            # Get query embedding
            embedding = await self._get_query_embedding(query)
            if not embedding:
                logger.warning("Could not generate query embedding")
                return []

            from sqlalchemy import text

            async with self._async_session() as session:
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

    async def _splade_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[SearchResult]:
        """Search using SPLADE learned sparse retrieval (optional)."""
        if not self._splade:
            return []
        try:
            results = await self._splade.search(query=query, limit=limit, path_filter=path_filter)
            return [
                SearchResult(
                    path=r.path,
                    chunk_index=r.chunk_index,
                    chunk_text=r.chunk_text,
                    score=r.score,
                    search_type="splade",
                )
                for r in results
            ]
        except Exception as e:
            logger.debug(f"SPLADE search failed: {e}")
            return []

    async def _hybrid_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
    ) -> list[SearchResult]:
        """Hybrid search combining keyword, semantic, and optionally SPLADE results.

        Pipeline: BM25 + Dense + SPLADE(optional) -> N-way RRF -> results
        """
        from nexus.bricks.search.fusion import (
            FusionConfig,
            FusionMethod,
            fuse_results,
            rrf_multi_fusion,
        )

        def _to_dicts(results: list[SearchResult]) -> list[dict[str, Any]]:
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

        # Run all retrieval backends in parallel
        tasks: list[asyncio.Task] = [
            asyncio.ensure_future(self._keyword_search(query, limit * 3, path_filter)),
            asyncio.ensure_future(self._semantic_search(query, limit * 3, path_filter)),
        ]
        has_splade = self._splade is not None
        if has_splade:
            tasks.append(asyncio.ensure_future(self._splade_search(query, limit * 3, path_filter)))

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle errors
        kw_results: list[SearchResult] = []
        sem_results: list[SearchResult] = []
        splade_results: list[SearchResult] = []

        if isinstance(raw_results[0], BaseException):
            logger.warning(f"Keyword search failed: {raw_results[0]}")
        else:
            kw_results = raw_results[0]
        if isinstance(raw_results[1], BaseException):
            logger.warning(f"Semantic search failed: {raw_results[1]}")
        else:
            sem_results = raw_results[1]
        if has_splade and len(raw_results) > 2:
            if isinstance(raw_results[2], BaseException):
                logger.warning(f"SPLADE search failed: {raw_results[2]}")
            else:
                splade_results = raw_results[2]

        keyword_dicts = _to_dicts(kw_results)
        semantic_dicts = _to_dicts(sem_results)
        splade_dicts = _to_dicts(splade_results)

        # Use N-way RRF when SPLADE is available, else standard 2-way fusion
        if splade_dicts:
            fused = rrf_multi_fusion(
                [
                    ("keyword", keyword_dicts),
                    ("vector", semantic_dicts),
                    ("splade", splade_dicts),
                ],
                k=60,
                limit=limit,
                id_key=None,
            )
        else:
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
                splade_score=r.get("splade_score"),
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
            if self._zoekt_client is None or not await self._zoekt_client.is_available():
                return []

            # Build Zoekt query
            zoekt_query = query
            if path_filter:
                zoekt_query = f"file:{path_filter.lstrip('/')} {zoekt_query}"

            matches = await self._zoekt_client.search(zoekt_query, num=limit)

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
        if not self._async_engine or not self._async_session:
            return []

        try:
            from sqlalchemy import text

            async with self._async_session() as session:
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
            from nexus.bricks.search.embeddings import create_embedding_provider

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
        """Refresh indexes for a batch of changed files.

        Issue #1024: Uses entropy-aware chunking to filter redundant content
        before indexing to BM25S.
        """
        logger.debug(f"Refreshing indexes for {len(paths)} files")

        if not self._bm25s_index or not self._file_reader:
            logger.debug("BM25S index or file reader not available, skipping refresh")
            return

        indexed_count = 0
        filtered_chunks = 0
        total_chunks = 0

        for path in paths:
            try:
                # Read file content (Issue #1520: FileReaderProtocol returns str)
                content = self._file_reader.read_text(path)
                if not content:
                    continue

                # Get path_id for indexing
                path_id = path  # Use path as ID for now

                # Apply entropy filtering if enabled (Issue #1024)
                if self._entropy_chunker:
                    result = await self._entropy_chunker.chunk_with_filtering(
                        content, path, compute_lines=True
                    )
                    total_chunks += result.original_count
                    filtered_chunks += result.original_count - result.filtered_count

                    # Index only the filtered (non-redundant) content
                    if result.chunks:
                        # Combine filtered chunks for BM25S indexing
                        filtered_content = "\n\n".join(c.text for c in result.chunks)
                        await self._bm25s_index.index_document(path_id, path, filtered_content)
                        indexed_count += 1
                else:
                    # Index full content without filtering
                    await self._bm25s_index.index_document(path_id, path, content)
                    indexed_count += 1

            except Exception as e:
                logger.warning(f"Failed to refresh index for {path}: {e}")

        if indexed_count > 0:
            logger.info(
                f"[DAEMON] Indexed {indexed_count}/{len(paths)} files"
                + (
                    f", filtered {filtered_chunks}/{total_chunks} redundant chunks"
                    if self._entropy_chunker
                    else ""
                )
            )

        # Update BM25S document count (include delta index)
        if self._bm25s_index:
            corpus_len = (
                len(self._bm25s_index._corpus) if hasattr(self._bm25s_index, "_corpus") else 0
            )
            delta_len = (
                len(self._bm25s_index._delta_corpus)
                if hasattr(self._bm25s_index, "_delta_corpus")
                else 0
            )
            self.stats.bm25_documents = corpus_len + delta_len

    # =========================================================================
    # Bulk Embedding (decoupled from BM25)
    # =========================================================================

    async def bulk_embed_from_bm25s(self, batch_size: int = 50) -> int:
        """Bulk-generate embeddings for documents found in the BM25S index.

        BM25 and embedding are independent search backends — this method
        uses the BM25S corpus only as a convenient content source to discover
        which files need embedding.  The actual flow:

        1. Read file list + content from BM25S metadata
        2. Register files in file_paths (the canonical file registry)
        3. Embed via the standard IndexingPipeline (document_chunks)

        Args:
            batch_size: Number of documents per embedding batch

        Returns:
            Number of documents successfully embedded
        """
        import uuid
        from datetime import datetime

        if not self._bm25s_index:
            logger.warning("[BULK-EMBED] No BM25S index available")
            return 0
        if not self._indexing_pipeline:
            logger.warning("[BULK-EMBED] No indexing pipeline available")
            return 0
        if not self._async_session:
            logger.warning("[BULK-EMBED] No database session available")
            return 0

        # Read BM25S corpus
        corpus = getattr(self._bm25s_index, "_corpus", [])
        paths = getattr(self._bm25s_index, "_paths", [])
        total = len(corpus)
        if total == 0:
            logger.info("[BULK-EMBED] BM25S corpus is empty")
            return 0

        logger.info(f"[BULK-EMBED] Processing {total} documents from BM25S corpus")

        # Step 1: Build deterministic UUID5 path_ids for each virtual path
        ns = uuid.UUID("12345678-1234-5678-1234-567812345678")
        path_id_map: dict[str, str] = {}
        now = datetime.utcnow()

        unique_vpaths: list[str] = []
        for i in range(total):
            vpath = paths[i] if i < len(paths) else f"doc_{i}"
            if vpath not in path_id_map:
                path_id_map[vpath] = str(uuid.uuid5(ns, vpath))
                unique_vpaths.append(vpath)

        # Query which paths already exist in file_paths
        from sqlalchemy import text

        existing_pids: dict[str, str] = {}
        async with self._async_session() as session:
            for batch_start in range(0, len(unique_vpaths), 100):
                batch_vpaths = unique_vpaths[batch_start : batch_start + 100]
                result = await session.execute(
                    text(
                        "SELECT path_id, virtual_path FROM file_paths "
                        "WHERE virtual_path = ANY(:vpaths) AND deleted_at IS NULL"
                    ),
                    {"vpaths": batch_vpaths},
                )
                for row in result.fetchall():
                    existing_pids[row[1]] = row[0]

        # Use existing path_ids where they exist
        for vpath, existing_pid in existing_pids.items():
            path_id_map[vpath] = existing_pid

        # Insert only new files into file_paths
        new_vpaths = [v for v in unique_vpaths if v not in existing_pids]
        if new_vpaths:
            async with self._async_session() as session:
                for vpath in new_vpaths:
                    pid = path_id_map[vpath]
                    await session.execute(
                        text(
                            "INSERT INTO file_paths (path_id, virtual_path, zone_id, created_at, updated_at) "
                            "VALUES (:pid, :vpath, 'default', :now, :now) "
                            "ON CONFLICT (path_id) DO NOTHING"
                        ),
                        {"pid": pid, "vpath": vpath, "now": now},
                    )
                await session.commit()
            logger.info(f"[BULK-EMBED] Registered {len(new_vpaths)} new files in file_paths")

        # Step 2: Feed to standard indexing pipeline with UUID path_ids
        docs_to_embed: list[tuple[str, str, str]] = []
        for i in range(total):
            vpath = paths[i] if i < len(paths) else f"doc_{i}"
            content = corpus[i]
            embed_pid = path_id_map.get(vpath)
            if embed_pid is not None and content:
                docs_to_embed.append((vpath, content, embed_pid))

        embedded = 0
        for batch_start in range(0, len(docs_to_embed), batch_size):
            batch = docs_to_embed[batch_start : batch_start + batch_size]
            try:
                await self._indexing_pipeline.index_documents(
                    [(path_id, vpath, content) for vpath, content, path_id in batch]
                )
                embedded += len(batch)
                if batch_start % (batch_size * 5) == 0:
                    logger.info(f"[BULK-EMBED] Progress: {embedded}/{len(docs_to_embed)}")
            except Exception as e:
                logger.warning(f"[BULK-EMBED] Batch failed at {batch_start}: {e}")

        logger.info(f"[BULK-EMBED] Complete: {embedded}/{len(docs_to_embed)} documents embedded")
        return embedded

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
            # Issue #1024: Entropy filtering configuration
            "entropy_filtering": {
                "enabled": self.config.entropy_filtering,
                "threshold": self.config.entropy_threshold,
                "alpha": self.config.entropy_alpha,
            },
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


async def create_and_start_daemon(
    database_url: str | None = None,
    bm25s_index_dir: str | None = None,
    *,
    async_session_factory: Any | None = None,
) -> SearchDaemon:
    """Create, configure and start a search daemon.

    Convenience function for creating a fully initialized daemon.

    Args:
        database_url: Database URL (from env if not provided)
        bm25s_index_dir: BM25S index directory
        async_session_factory: Injected async_sessionmaker from RecordStoreABC.

    Returns:
        Initialized SearchDaemon instance
    """
    config = DaemonConfig(
        database_url=database_url or get_database_url(),
        bm25s_index_dir=bm25s_index_dir or ".nexus-data/bm25s",
    )

    daemon = SearchDaemon(config, async_session_factory=async_session_factory)
    await daemon.startup()
    return daemon
