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

    # txtai backend config (Issue #2663)
    txtai_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    txtai_reranker: str | None = None  # e.g. "cross-encoder/ms-marco-MiniLM-L-2-v2"
    txtai_sparse: bool = False  # Enable SPLADE learned sparse retrieval
    txtai_graph: bool = True  # Enable semantic graph


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
        cache_brick: Any | None = None,
    ):
        """Initialize the search daemon.

        Args:
            config: Daemon configuration (uses defaults if not provided)
            async_session_factory: Injected async_sessionmaker from RecordStoreABC.
                When provided, skips creating a private engine (Issue #1597).
            zoekt_client: Injected ZoektClient instance (Issue #2188).
            cache_brick: Injected CacheBrick for embedding cache health checks.
        """
        self.config = config or DaemonConfig()
        self.stats = DaemonStats()
        self._zoekt_client = zoekt_client
        self._cache_brick = cache_brick

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
        self._pending_delete_paths: set[str] = set()
        self._refresh_lock = asyncio.Lock()

        # FileReaderProtocol reference for reading file content (set by FastAPI server)
        # Issue #1520: Replaces direct NexusFS dependency
        self._file_reader: Any = None

        # Issue #2036: Injected adaptive-k provider (replaces lazy import)
        self._adaptive_k_provider: Any = None

        # SPLADE learned sparse retrieval (optional, initialized in startup)
        self._splade: Any = None

        # txtai backend (Issue #2663) — used for semantic/hybrid search + graph
        self._backend: Any = None
        self.last_search_timing: dict[str, float] = {}

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

        # Initialize txtai backend for semantic/hybrid/graph search (Issue #2663)
        try:
            from nexus.bricks.search.txtai_backend import TxtaiBackend

            self._backend = TxtaiBackend(
                database_url=self.config.database_url,
                model=self.config.txtai_model,
                hybrid=True,
                graph=self.config.txtai_graph,
                reranker_model=self.config.txtai_reranker,
                sparse=self.config.txtai_sparse,
            )
            await self._backend.startup()
            logger.info("txtai backend initialized successfully")
        except Exception:
            logger.warning(
                "txtai backend init failed, falling back to legacy search", exc_info=True
            )
            self._backend = None

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

        _db_type = (
            "postgresql"
            if self.config.database_url and "postgresql" in self.config.database_url
            else "sqlite"
        )
        self._indexing_pipeline = _IP(
            chunker=DocumentChunker(),
            embedding_provider=self._embedding_provider,
            entropy_chunker=self._entropy_chunker,
            db_type=_db_type,
            async_session_factory=self._async_session,
            max_concurrency=self.config.max_indexing_concurrency,
            cross_doc_batching=True,
        )

        # Start index refresh background task
        if self.config.refresh_enabled:
            self._refresh_task = asyncio.create_task(self._index_refresh_loop())

        # If BM25S not loaded, count existing DB documents for stats
        if not self._bm25s_index and self._async_session:
            try:
                from sqlalchemy import text as sa_text

                session_factory = self._async_session
                async with session_factory() as sess:
                    row = (
                        await sess.execute(
                            sa_text("SELECT COUNT(DISTINCT path_id) FROM document_chunks")
                        )
                    ).first()
                    self.stats.bm25_documents = int(row[0]) if row else 0
            except Exception:
                logger.debug("Could not count DB documents at startup")

        self._initialized = True
        self.stats.startup_time_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            "SearchDaemon ready in %.1fms - keyword docs: %d, DB pool: %d connections",
            self.stats.startup_time_ms,
            self.stats.bm25_documents,
            self.stats.db_pool_size,
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

        # Shutdown txtai backend (Issue #2663)
        if self._backend is not None:
            try:
                await self._backend.shutdown()
            except Exception as e:
                logger.debug("txtai backend shutdown error: %s", e)
            self._backend = None

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
            # Extract engine reference from the injected session factory
            # so db_pool_ready reports correctly.
            _bind = getattr(self._async_session, "kw", {}).get("bind")
            if _bind is not None:
                self._async_engine = _bind
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
        except Exception as e:
            logger.debug("Zoekt availability check failed: %s", e)
            self.stats.zoekt_available = False

    async def _check_embedding_cache(self) -> None:
        """Check if embedding cache (Dragonfly) is connected."""
        if self._cache_brick is not None:
            try:
                self.stats.embedding_cache_connected = await self._cache_brick.health_check()
                return
            except Exception as e:
                logger.debug("Embedding cache health check failed: %s", e)
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
        zone_id: str | None = None,
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

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID
        start = time.perf_counter()
        self.last_search_timing = {}

        try:
            # For keyword search: Zoekt first (code search), then txtai BM25
            if search_type == "keyword" and self.stats.zoekt_available:
                zoekt_results = await self._search_zoekt(query, limit, path_filter)
                if zoekt_results:
                    latency_ms = (time.perf_counter() - start) * 1000
                    self._track_latency(latency_ms)
                    self.last_search_timing["backend_ms"] = latency_ms
                    return zoekt_results

            # Delegate to txtai backend for all search types (Issue #2663)
            if self._backend is not None:
                backend_start = time.perf_counter()
                backend_results = await self._backend.search(
                    query,
                    limit=limit,
                    zone_id=effective_zone_id,
                    search_type=search_type,
                    path_filter=path_filter,
                )
                backend_ms = (time.perf_counter() - backend_start) * 1000
                rerank_ms = getattr(self._backend, "last_rerank_ms", 0.0)
                self.last_search_timing = {
                    "backend_ms": backend_ms,
                    "rerank_ms": rerank_ms,
                }

                if backend_results:
                    results = [
                        SearchResult(
                            path=r.path,
                            chunk_index=r.chunk_index,
                            chunk_text=r.chunk_text,
                            score=r.score,
                            start_offset=r.start_offset,
                            end_offset=r.end_offset,
                            line_start=r.line_start,
                            line_end=r.line_end,
                            keyword_score=r.keyword_score,
                            vector_score=r.vector_score,
                            reranker_score=r.reranker_score,
                            search_type=search_type,
                        )
                        for r in backend_results
                    ]

                    latency_ms = (time.perf_counter() - start) * 1000
                    self._track_latency(latency_ms)
                    return results
                # txtai returned empty — fall through to legacy search

            # Legacy fallback (txtai backend not available or returned no results)
            if search_type == "keyword":
                results = await self._keyword_search(query, limit, path_filter, zone_id=zone_id)
            elif search_type == "semantic":
                results = await self._semantic_search(query, limit, path_filter, zone_id=zone_id)
            else:  # hybrid
                results = await self._hybrid_search(
                    query,
                    limit,
                    path_filter,
                    alpha,
                    fusion_method,
                    zone_id=zone_id,
                )

            # Track latency
            latency_ms = (time.perf_counter() - start) * 1000
            self._track_latency(latency_ms)

            return results

        except TimeoutError:
            logger.warning(f"Search timeout after {self.config.query_timeout_seconds}s")
            return []

    async def index_documents(
        self,
        documents: list[dict[str, Any]],
        *,
        zone_id: str | None = None,
    ) -> int:
        """Explicitly upsert documents into the active search backend.

        This powers ``POST /api/v2/search/index`` for synthetic or externally
        generated documents that do not rely on the file-refresh pipeline.
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        if not documents or self._backend is None:
            return 0

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID
        count = int(await self._backend.upsert(documents, zone_id=effective_zone_id))
        if count:
            self.stats.last_index_refresh = time.time()
        return count

    async def delete_documents(
        self,
        ids: list[str],
        *,
        zone_id: str | None = None,
    ) -> int:
        """Delete indexed documents from the active search backend."""
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        if not ids or self._backend is None:
            return 0

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID
        count = int(await self._backend.delete(ids, zone_id=effective_zone_id))
        if count:
            self.stats.last_index_refresh = time.time()
        return count

    async def _keyword_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
        *,
        zone_id: str | None = None,
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
            return await self._search_fts(query, limit, path_filter, zone_id=zone_id)

        return results

    async def _semantic_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
        *,
        zone_id: str | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search using pgvector."""
        if not self._async_engine or not self._async_session:
            logger.warning("Semantic search requires database connection")
            return []

        try:
            # Get query embedding
            embedding = await self._get_query_embedding(query)
            if not embedding:
                if self._embedding_provider is None:
                    logger.debug(
                        "Legacy semantic search unavailable: no embedding provider configured"
                    )
                else:
                    logger.warning("Could not generate query embedding")
                return []

            from sqlalchemy import text

            # Convert embedding list to pgvector string format for asyncpg
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

            async with self._async_session() as session:
                # Build WHERE clause dynamically to avoid asyncpg
                # AmbiguousParameterError with IS NULL patterns
                where_parts = ["c.embedding IS NOT NULL"]
                params: dict[str, Any] = {
                    "embedding": embedding_str,
                    "limit": limit,
                }
                if path_filter:
                    where_parts.append("fp.virtual_path LIKE :path_pattern")
                    params["path_pattern"] = f"{path_filter}%"
                if zone_id:
                    where_parts.append("fp.zone_id = :zone_id")
                    params["zone_id"] = zone_id

                where_clause = " AND ".join(where_parts)
                sql = text(f"""
                    SELECT
                        c.chunk_index, c.chunk_text,
                        c.start_offset, c.end_offset, c.line_start, c.line_end,
                        fp.virtual_path,
                        1 - (c.embedding <=> CAST(:embedding AS halfvec)) as score
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE {where_clause}
                    ORDER BY c.embedding <=> CAST(:embedding AS halfvec)
                    LIMIT :limit
                """)

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
        alpha: float,  # noqa: ARG002
        fusion_method: str,  # noqa: ARG002
        *,
        zone_id: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid search combining keyword and semantic results via RRF.

        Pipeline: BM25/Zoekt + Dense -> RRF fusion -> results
        """
        # Run keyword and semantic in parallel
        kw_task = asyncio.ensure_future(
            self._keyword_search(query, limit * 3, path_filter, zone_id=zone_id)
        )
        sem_task = asyncio.ensure_future(
            self._semantic_search(query, limit * 3, path_filter, zone_id=zone_id)
        )

        raw_results = await asyncio.gather(kw_task, sem_task, return_exceptions=True)

        kw_results: list[SearchResult] = []
        sem_results: list[SearchResult] = []

        if isinstance(raw_results[0], BaseException):
            logger.warning("Keyword search failed: %s", raw_results[0])
        else:
            kw_results = raw_results[0]
        if isinstance(raw_results[1], BaseException):
            logger.warning("Semantic search failed: %s", raw_results[1])
        else:
            sem_results = raw_results[1]

        # RRF fusion (k=60)
        rrf_k = 60
        scores: dict[str, float] = {}
        best: dict[str, SearchResult] = {}

        for rank, r in enumerate(kw_results):
            key = f"{r.path}:{r.chunk_index}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            if key not in best:
                best[key] = r

        for rank, r in enumerate(sem_results):
            key = f"{r.path}:{r.chunk_index}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            if key not in best:
                best[key] = r

        # Sort by fused score, take top limit
        sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)[:limit]

        return [
            SearchResult(
                path=best[k].path,
                chunk_text=best[k].chunk_text,
                score=scores[k],
                chunk_index=best[k].chunk_index,
                start_offset=best[k].start_offset,
                end_offset=best[k].end_offset,
                line_start=best[k].line_start,
                line_end=best[k].line_end,
                keyword_score=best[k].keyword_score,
                vector_score=best[k].vector_score,
                search_type="hybrid",
            )
            for k in sorted_keys
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
        *,
        zone_id: str | None = None,
    ) -> list[SearchResult]:
        """Search using database FTS (fallback)."""
        if not self._async_engine or not self._async_session:
            return []

        try:
            from sqlalchemy import text

            async with self._async_session() as session:
                # Build WHERE clause dynamically to avoid asyncpg
                # AmbiguousParameterError with IS NULL patterns
                where_parts = [
                    "to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)"
                ]
                params: dict[str, Any] = {"query": query, "limit": limit}
                if path_filter:
                    where_parts.append("fp.virtual_path LIKE :path_pattern")
                    params["path_pattern"] = f"{path_filter}%"
                if zone_id:
                    where_parts.append("fp.zone_id = :zone_id")
                    params["zone_id"] = zone_id

                where_clause = " AND ".join(where_parts)
                sql = text(f"""
                    SELECT
                        c.chunk_index, c.chunk_text,
                        c.start_offset, c.end_offset, c.line_start, c.line_end,
                        fp.virtual_path,
                        ts_rank(to_tsvector('english', c.chunk_text), plainto_tsquery('english', :query)) as score
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE {where_clause}
                    ORDER BY score DESC
                    LIMIT :limit
                """)

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
        """Get embedding for query text (legacy fallback path).

        Note: With txtai backend active, this method is only called
        by the legacy _semantic_search path which is bypassed.
        """
        if self._embedding_provider:
            result = await self._embedding_provider.embed_text(query)
            return list(result) if result else None

        logger.debug("No embedding provider available for legacy search path")
        return None

    # =========================================================================
    # Index Refresh
    # =========================================================================

    async def notify_file_change(self, path: str, change_type: str = "update") -> None:
        """Notify the daemon of a file change for index refresh.

        Changes are debounced and batched for efficiency.

        Args:
            path: File path that changed
            change_type: Type of change ("create", "update", "delete")
        """
        if not self.config.refresh_enabled:
            return

        async with self._refresh_lock:
            if change_type == "delete":
                self._pending_delete_paths.add(path)
                self._pending_refresh_paths.discard(path)
            else:
                self._pending_refresh_paths.add(path)
                self._pending_delete_paths.discard(path)

    async def _index_refresh_loop(self) -> None:
        """Background task to refresh indexes for changed files."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(self.config.refresh_debounce_seconds)

                async with self._refresh_lock:
                    if not self._pending_refresh_paths and not self._pending_delete_paths:
                        continue

                    paths = list(self._pending_refresh_paths)
                    self._pending_refresh_paths.clear()
                    delete_paths = list(self._pending_delete_paths)
                    self._pending_delete_paths.clear()

                # Delete removed files from the index (IDs = unscoped virtual_path)
                if delete_paths and self._backend is not None:
                    from nexus.contracts.constants import ROOT_ZONE_ID

                    unscoped = [self._strip_zone_prefix(p) for p in delete_paths]
                    await self.delete_documents(unscoped, zone_id=ROOT_ZONE_ID)

                # Refresh indexes for changed paths
                if paths:
                    await self._refresh_indexes(paths)
                self.stats.last_index_refresh = time.time()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Index refresh error: {e}")

    @staticmethod
    def _strip_zone_prefix(path: str) -> str:
        """Strip /zone/{zone_id} prefix from a path for DB virtual_path lookup.

        DB stores virtual_path as '/test-search/...' not '/zone/root/test-search/...'.
        """
        import re

        m = re.match(r"^/zone/[^/]+(/.*)", path)
        return m.group(1) if m else path

    async def _index_to_document_chunks(self, path_id: str, path: str, content: str) -> None:
        """Insert content as document_chunks for FTS search."""
        if not self._async_session:
            return

        try:
            import uuid
            from datetime import UTC, datetime

            from sqlalchemy import text as sa_text

            # Split into chunks (~1000 chars each for search granularity)
            chunk_size = 1000
            chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
            now = datetime.now(UTC).replace(tzinfo=None)

            async with self._async_session() as sess:
                # Delete existing chunks for this path
                await sess.execute(
                    sa_text("DELETE FROM document_chunks WHERE path_id = :pid"),
                    {"pid": path_id},
                )

                for idx, chunk_text in enumerate(chunks):
                    if not chunk_text.strip():
                        continue
                    # Compute approximate line numbers
                    preceding = content[: idx * chunk_size]
                    line_start = preceding.count("\n") + 1
                    line_end = line_start + chunk_text.count("\n")
                    # Approximate token count (~4 chars per token)
                    chunk_tokens = max(1, len(chunk_text) // 4)

                    await sess.execute(
                        sa_text(
                            "INSERT INTO document_chunks "
                            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, "
                            "start_offset, end_offset, line_start, line_end, created_at) "
                            "VALUES (:cid, :pid, :idx, :txt, :tokens, "
                            ":s_off, :e_off, :ls, :le, :created)"
                        ),
                        {
                            "cid": str(uuid.uuid4()),
                            "pid": path_id,
                            "idx": idx,
                            "txt": chunk_text,
                            "tokens": chunk_tokens,
                            "s_off": idx * chunk_size,
                            "e_off": idx * chunk_size + len(chunk_text),
                            "ls": line_start,
                            "le": line_end,
                            "created": now,
                        },
                    )
                await sess.commit()
        except Exception as e:
            logger.debug("Failed to index %s to document_chunks: %s", path, e)

    async def _refresh_indexes(self, paths: list[str]) -> None:
        """Refresh indexes for a batch of changed files.

        Indexes to both BM25S (if available) and database document_chunks
        (for FTS fallback). File content is read via _file_reader or
        from the content_cache table.
        """
        logger.debug("Refreshing indexes for %d files", len(paths))

        if not self._file_reader and not self._async_session:
            logger.debug("No file reader or DB session, skipping refresh")
            return

        indexed_count = 0
        # Collect documents for batched txtai upsert (Issue #2663)
        _txtai_batch: dict[str, list[dict]] = {}  # zone_id -> docs

        for path in paths:
            try:
                # Strip /zone/{zone_id} prefix for DB virtual_path lookup
                virtual_path = self._strip_zone_prefix(path)

                # Read file content via file reader or database
                content: str | None = None
                if self._file_reader:
                    import contextlib

                    try:
                        content = self._file_reader.read_text(path)
                    except Exception as e:
                        logger.debug("File read failed for %s: %s, trying virtual path", path, e)
                        # Also try without zone prefix — best-effort fallback
                        with contextlib.suppress(OSError, ValueError):
                            content = self._file_reader.read_text(virtual_path)

                # Fallback: read from content_cache table
                if not content and self._async_session:
                    try:
                        from sqlalchemy import text as sa_text

                        async with self._async_session() as sess:
                            row = (
                                await sess.execute(
                                    sa_text(
                                        "SELECT cc.content_text FROM content_cache cc "
                                        "JOIN file_paths fp ON cc.path_id = fp.path_id "
                                        "WHERE fp.virtual_path = :vp "
                                        "AND cc.content_text IS NOT NULL "
                                        "LIMIT 1"
                                    ),
                                    {"vp": virtual_path},
                                )
                            ).first()
                            if row and row[0]:
                                content = str(row[0])
                    except Exception as db_err:
                        logger.debug("DB content read failed for %s: %s", path, db_err)

                if not content:
                    logger.debug("No content found for %s", path)
                    continue

                # Resolve path_id from file_paths table
                path_id = virtual_path  # fallback
                if self._async_session:
                    try:
                        from sqlalchemy import text as sa_text

                        async with self._async_session() as sess:
                            row = (
                                await sess.execute(
                                    sa_text(
                                        "SELECT path_id FROM file_paths "
                                        "WHERE virtual_path = :vp LIMIT 1"
                                    ),
                                    {"vp": virtual_path},
                                )
                            ).first()
                            if row:
                                path_id = row[0]
                    except Exception as e:
                        logger.debug("path_id lookup failed for %s: %s", virtual_path, e)

                # Index to BM25S if available
                if self._bm25s_index:
                    await self._bm25s_index.index_document(path_id, path, content)

                # Index to database document_chunks for FTS
                if self._async_session:
                    await self._index_to_document_chunks(path_id, path, content)

                indexed_count += 1

                # Collect for batched txtai upsert (Issue #2663)
                if self._backend is not None:
                    import re as _re

                    _zm = _re.match(r"^/zone/([^/]+)/", path)
                    _zone = _zm.group(1) if _zm else "root"
                    _txtai_batch.setdefault(_zone, []).append(
                        {
                            "id": virtual_path,
                            "text": content,
                            "path": virtual_path,
                            "zone_id": _zone,
                        }
                    )

                # Also run indexing pipeline for chunk + embedding storage
                if self._indexing_pipeline and self._embedding_provider:
                    try:
                        await self._indexing_pipeline.index_document(path, content, path_id)
                    except Exception as ie:
                        logger.debug("Indexing pipeline error for %s: %s", path, ie)

            except Exception as e:
                logger.warning("Failed to refresh index for %s: %s", path, e)

        # Batched txtai upsert — one call per zone with all docs (Issue #2663)
        if self._backend is not None and _txtai_batch:
            for zone_id, docs in _txtai_batch.items():
                try:
                    await self._backend.upsert(docs, zone_id=zone_id)
                    logger.debug("txtai batch upsert: %d docs for zone %s", len(docs), zone_id)
                except Exception as te:
                    logger.warning("txtai batch upsert failed for zone %s: %s", zone_id, te)

        if indexed_count > 0:
            logger.info("[DAEMON] Indexed %d/%d files", indexed_count, len(paths))

        # Update document count (BM25S index or DB FTS fallback)
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
        elif self._async_session and indexed_count > 0:
            # BM25S not available — count from DB document_chunks table
            session_factory = self._async_session
            try:
                from sqlalchemy import text as sa_text

                async with session_factory() as sess:
                    row = (
                        await sess.execute(
                            sa_text("SELECT COUNT(DISTINCT path_id) FROM document_chunks")
                        )
                    ).first()
                    self.stats.bm25_documents = int(row[0]) if row else 0
            except Exception:
                # If count fails, at least track that we indexed something
                self.stats.bm25_documents += indexed_count

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
        from datetime import UTC, datetime

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
        now = datetime.now(UTC).replace(tzinfo=None)

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
                    [(vpath, content, path_id) for vpath, content, path_id in batch]
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
            # Issue #2663: txtai backend
            "backend": "txtai" if self._backend is not None else "legacy",
            "txtai_model": self.config.txtai_model,
            "txtai_reranker": self.config.txtai_reranker,
            "txtai_graph": self.config.txtai_graph,
        }

    def get_health(self) -> dict[str, Any]:
        """Get health status for health check endpoint.

        Returns:
            Health status dictionary
        """
        # Keyword search is available via BM25S, Zoekt, or DB FTS fallback
        keyword_ready = (
            self._bm25s_index is not None
            or self.stats.zoekt_available
            or self._async_engine is not None
        )
        return {
            "status": "healthy" if self._initialized else "starting",
            "initialized": self._initialized,
            "daemon_initialized": self._initialized,
            "backend": "txtai" if self._backend is not None else "legacy",
            "bm25_index_loaded": keyword_ready,
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
