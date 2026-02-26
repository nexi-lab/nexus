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
import re
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

# Zone prefix pattern: /zone/{zone_id}/rest/of/path
_ZONE_PREFIX_RE = re.compile(r"^/zone/[^/]+/")


def _strip_zone_prefix(path: str) -> str:
    """Strip the /zone/{zone_id}/ prefix from a virtual path.

    Search results store paths with zone prefix (e.g. /zone/corp/foo.md)
    but the API returns user-facing paths without it (e.g. /foo.md).
    """
    return _ZONE_PREFIX_RE.sub("/", path)


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
    # QMD pipeline per-stage latencies (last query)
    pipeline_stage_latencies: dict[str, float] | None = None


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

    # Embedding provider for query-time vectors
    embedding_provider: str = "openai"  # "openai" | "fastembed" | "voyage"
    embedding_model: str = "text-embedding-3-small"  # must match stored embeddings dim

    # QMD pipeline stages (all optional, all default OFF for backward compat)
    query_expansion_enabled: bool = False
    expansion_provider: str = "openrouter"  # "openrouter" | "local"
    expansion_model: str = "deepseek/deepseek-chat"  # or GGUF path for local
    reranking_enabled: bool = False
    reranker_provider: str = "local"  # "local" | "jina" | "cohere"
    reranker_model: str = "jina-tiny"  # key from RERANKER_MODELS
    reranking_top_k: int = 30  # max candidates to rerank
    position_aware_blending: bool = True  # only active when reranking_enabled
    scored_chunking_enabled: bool = False  # scored break-point chunking


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
        self._refresh_lock = asyncio.Lock()

        # FileReaderProtocol reference for reading file content (set by FastAPI server)
        # Issue #1520: Replaces direct NexusFS dependency
        self._file_reader: Any = None

        # Issue #2036: Injected adaptive-k provider (replaces lazy import)
        self._adaptive_k_provider: Any = None

        # SPLADE learned sparse retrieval (optional, initialized in startup)
        self._splade: Any = None

        # QMD pipeline components (initialized on startup if enabled)
        # Runtime flags track whether each stage is actually active.
        # Config is never mutated — these decouple intent from runtime state.
        self._reranker: Any = None
        self._reranking_active = False
        self._expansion_service: Any = None
        self._expansion_active = False

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

        # Initialize embedding provider for query-time vectors
        try:
            from nexus.bricks.search.embeddings import create_embedding_provider

            self._embedding_provider = create_embedding_provider(
                provider=self.config.embedding_provider,
                model=self.config.embedding_model,
            )
            _dim = self._embedding_provider.embedding_dimension()
            logger.info(
                "Embedding provider: %s/%s (%dD)",
                self.config.embedding_provider,
                self.config.embedding_model,
                _dim,
            )
        except Exception as e:
            logger.warning("Embedding provider init failed (%s), semantic search disabled", e)

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

        # Detect database type from URL or engine dialect for correct bulk insert path
        _db_url = self.config.database_url or ""
        _is_pg = "postgresql" in _db_url or "postgres" in _db_url
        if not _is_pg and self._async_engine is not None:
            _is_pg = self._async_engine.dialect.name == "postgresql"
        _db_type = "postgresql" if _is_pg else "sqlite"

        self._indexing_pipeline = _IP(
            chunker=DocumentChunker(),
            embedding_provider=self._embedding_provider,
            entropy_chunker=self._entropy_chunker,
            db_type=_db_type,
            async_session_factory=self._async_session,
            max_concurrency=self.config.max_indexing_concurrency,
            cross_doc_batching=True,
        )

        # QMD pipeline: Initialize reranker if enabled
        if self.config.reranking_enabled:
            try:
                from nexus.bricks.search.mobile_config import RERANKER_MODELS
                from nexus.bricks.search.mobile_providers import create_reranker_provider

                model_config = RERANKER_MODELS.get(self.config.reranker_model)
                if model_config:
                    self._reranker = await create_reranker_provider(model_config)
                    self._reranking_active = True
                    logger.info("Reranker loaded: %s", self.config.reranker_model)
                else:
                    logger.warning(
                        "Unknown reranker model: %s, disabling", self.config.reranker_model
                    )
            except Exception as e:
                logger.warning("Reranker init failed (%s), disabling reranking", e)

        # QMD pipeline: Initialize query expansion if enabled
        if self.config.query_expansion_enabled:
            try:
                from nexus.bricks.search.query_expansion import (
                    QueryExpansionConfig,
                    create_query_expansion_service,
                )

                exp_config = QueryExpansionConfig(
                    provider=self.config.expansion_provider,
                    model=self.config.expansion_model,
                )
                self._expansion_service = create_query_expansion_service(
                    provider=self.config.expansion_provider,
                    model=self.config.expansion_model,
                    config=exp_config,
                )
                self._expansion_active = True
                logger.info(
                    "Query expansion enabled: provider=%s, model=%s",
                    self.config.expansion_provider,
                    self.config.expansion_model,
                )
            except Exception as e:
                logger.warning("Query expansion init failed (%s), disabling", e)

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
        except Exception:
            self.stats.zoekt_available = False

    async def _check_embedding_cache(self) -> None:
        """Check if embedding cache (Dragonfly) is connected."""
        if self._cache_brick is not None:
            try:
                self.stats.embedding_cache_connected = await self._cache_brick.health_check()
                return
            except Exception:
                pass
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

        start = time.perf_counter()

        try:
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
                logger.warning("Could not generate query embedding")
                return []

            from sqlalchemy import text

            async with self._async_session() as session:
                # asyncpg cannot infer pgvector parameter types (CAST/::halfvec)
                # or NULL parameter types. Build SQL dynamically to avoid both
                # issues. Vector literal and path filter are safe to interpolate
                # (floats from our model, path from internal code).
                vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"

                # Build WHERE clauses dynamically to avoid asyncpg NULL
                # parameter type inference issues.
                where_clauses = ["c.embedding IS NOT NULL"]
                params: dict[str, Any] = {"limit": limit}

                if path_filter:
                    where_clauses.append("fp.virtual_path LIKE :path_pattern")
                    params["path_pattern"] = f"{path_filter}%"

                if zone_id and zone_id != "root":
                    where_clauses.append("fp.zone_id = :zone_id")
                    params["zone_id"] = zone_id

                where_sql = " AND ".join(where_clauses)

                sql = text(f"""
                    SELECT
                        c.chunk_index, c.chunk_text,
                        c.start_offset, c.end_offset, c.line_start, c.line_end,
                        fp.virtual_path,
                        1 - (c.embedding <=> '{vec_literal}'::halfvec) as score
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE {where_sql}
                    ORDER BY c.embedding <=> '{vec_literal}'::halfvec
                    LIMIT :limit
                """)

                result = await session.execute(sql, params)

                return [
                    SearchResult(
                        path=_strip_zone_prefix(row.virtual_path),
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
                    path=_strip_zone_prefix(r.path),
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
        *,
        zone_id: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid search combining keyword, semantic, and optionally SPLADE results.

        QMD-inspired pipeline:
        1. [Optional] Query expansion with strong signal detection
        2. Parallel keyword + semantic + SPLADE(optional) retrieval
        3. N-way weighted RRF fusion (or standard 2-way for backward compat)
        4. [Optional] Cross-encoder reranking
        5. [Optional] Position-aware blending
        """
        from nexus.bricks.search.fusion import (
            FusionConfig,
            FusionMethod,
            fuse_results,
            rrf_multi_fusion,
        )

        timings: dict[str, float] = {}
        over_fetch = limit * 3

        # Stage 1: Query expansion (if enabled)
        expansion_queries: list[Any] = []
        if self._expansion_active and self._expansion_service:
            t0 = time.perf_counter()
            try:
                # Quick BM25 probe for strong signal detection
                probe_results = await self._keyword_search(query, 5, path_filter, zone_id=zone_id)
                probe_dicts = [r.to_dict() for r in probe_results]

                expansion_result = await self._expansion_service.expand_if_needed(
                    query, initial_results=probe_dicts
                )
                if expansion_result.was_expanded:
                    expansion_queries = expansion_result.expansions
            except Exception as e:
                logger.warning("Query expansion failed (%s), continuing without", e)
            timings["expansion_ms"] = (time.perf_counter() - t0) * 1000

        # Stage 2: Parallel retrieval (keyword + semantic + optional SPLADE)
        t0 = time.perf_counter()
        tasks: list[asyncio.Task[Any]] = [
            asyncio.ensure_future(
                self._keyword_search(query, over_fetch, path_filter, zone_id=zone_id)
            ),
            asyncio.ensure_future(
                self._semantic_search(query, over_fetch, path_filter, zone_id=zone_id)
            ),
        ]
        has_splade = self._splade is not None
        if has_splade:
            tasks.append(asyncio.ensure_future(self._splade_search(query, over_fetch, path_filter)))

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle errors gracefully
        kw_results: list[SearchResult] = []
        sem_results: list[SearchResult] = []
        splade_results: list[SearchResult] = []

        if isinstance(raw_results[0], BaseException):
            logger.warning("Keyword search failed: %s", raw_results[0])
        else:
            kw_results = raw_results[0]
        if isinstance(raw_results[1], BaseException):
            logger.warning("Semantic search failed: %s", raw_results[1])
        else:
            sem_results = raw_results[1]
        if has_splade and len(raw_results) > 2:
            if isinstance(raw_results[2], BaseException):
                logger.warning("SPLADE search failed: %s", raw_results[2])
            else:
                splade_results = raw_results[2]

        timings["retrieval_ms"] = (time.perf_counter() - t0) * 1000

        # Convert to dicts for fusion (DRY: uses BaseSearchResult.to_dict())
        keyword_dicts = [r.to_dict() for r in kw_results]
        semantic_dicts = [r.to_dict() for r in sem_results]
        splade_dicts = [r.to_dict() for r in splade_results]

        # Stage 3: Fusion
        t0 = time.perf_counter()
        if expansion_queries or splade_dicts:
            # N-way weighted RRF: original queries weighted 2.0, expanded 1.0
            retrieval_sources: list[tuple[str, list[dict[str, Any]], float]] = [
                ("keyword_orig", keyword_dicts, 2.0),
                ("vector_orig", semantic_dicts, 2.0),
            ]

            # Include SPLADE results if available
            if splade_dicts:
                retrieval_sources.append(("splade", splade_dicts, 1.5))

            # Retrieve for each expanded query
            if expansion_queries:
                from nexus.bricks.search.query_expansion import ExpansionType

                for exp in expansion_queries:
                    try:
                        if exp.expansion_type == ExpansionType.LEX:
                            exp_results = await self._keyword_search(
                                exp.text, over_fetch, path_filter
                            )
                            exp_dicts = [r.to_dict() for r in exp_results]
                            retrieval_sources.append(("keyword_exp", exp_dicts, 1.0))
                        elif exp.expansion_type in (ExpansionType.VEC, ExpansionType.HYDE):
                            exp_results = await self._semantic_search(
                                exp.text, over_fetch, path_filter
                            )
                            exp_dicts = [r.to_dict() for r in exp_results]
                            retrieval_sources.append(("vector_exp", exp_dicts, 1.0))
                    except Exception as e:
                        logger.warning("Expanded query retrieval failed (%s)", e)

            fused = rrf_multi_fusion(
                retrieval_sources,
                k=60,
                limit=limit,
                id_key=None,
                top_rank_bonus=bool(expansion_queries),
            )
        else:
            # Standard 2-way fusion (backward compat)
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

        timings["fusion_ms"] = (time.perf_counter() - t0) * 1000

        # Stage 4: Reranking + position-aware blending
        if self._reranking_active and self._reranker:
            t0 = time.perf_counter()
            fused, reranker_scores = await self._rerank_results(fused, query)
            if self.config.position_aware_blending and reranker_scores:
                from nexus.bricks.search.fusion import position_aware_blend

                fused = position_aware_blend(fused, reranker_scores, id_key=None)
            timings["reranking_ms"] = (time.perf_counter() - t0) * 1000

        # Log pipeline timings
        if timings:
            self.stats.pipeline_stage_latencies = timings
            logger.info("[PIPELINE] %s", timings)

        # Convert back to SearchResult (DRY: uses from_dict())
        results: list[SearchResult] = []
        for r in fused:
            r["search_type"] = "hybrid"
            result = SearchResult.from_dict(r)
            results.append(result)
        return results

    async def _rerank_results(
        self,
        results: list[dict[str, Any]],
        query: str,
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        """Cross-encoder reranking of top candidates.

        Returns copies — never mutates the input results.
        Tail results beyond reranking_top_k are preserved in original order.

        Args:
            results: Fused results to rerank
            query: Original query text

        Returns:
            Tuple of (reranked candidates + tail, scores dict keyed by result ID)
        """
        from nexus.bricks.search.fusion import _get_result_key

        if not self._reranker or not results:
            return results, {}

        top_k = self.config.reranking_top_k
        candidates = results[:top_k]
        tail = results[top_k:]
        documents = [r.get("chunk_text", "") for r in candidates]

        try:
            ranked = await self._reranker.rerank(query, documents, top_k=len(documents))
            # ranked: list[tuple[int, float]] — (original_index, score)

            reranker_scores: dict[str, float] = {}
            for orig_idx, score in ranked:
                key = _get_result_key(candidates[orig_idx], id_key=None)
                reranker_scores[key] = score

            return candidates + tail, reranker_scores
        except Exception as e:
            logger.warning("Reranking failed (%s), returning unranked results", e)
            return results, {}

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
                    path=_strip_zone_prefix(match.file),
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
                    path=_strip_zone_prefix(r.path),
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
                # PostgreSQL FTS query with zone isolation
                sql = text("""
                    SELECT
                        c.chunk_index, c.chunk_text,
                        c.start_offset, c.end_offset, c.line_start, c.line_end,
                        fp.virtual_path,
                        ts_rank(to_tsvector('english', c.chunk_text), plainto_tsquery('english', :query)) as score
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
                      AND (:path_filter IS NULL OR fp.virtual_path LIKE :path_pattern)
                      AND (:zone_id IS NULL OR fp.zone_id = :zone_id)
                    ORDER BY score DESC
                    LIMIT :limit
                """)
                params = {
                    "query": query,
                    "limit": limit,
                    "path_filter": path_filter,
                    "path_pattern": f"{path_filter}%" if path_filter else None,
                    "zone_id": zone_id,
                }

                result = await session.execute(sql, params)

                return [
                    SearchResult(
                        path=_strip_zone_prefix(row.virtual_path),
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

        # Also generate embeddings for newly indexed documents (decoupled from BM25)
        if indexed_count > 0 and self._indexing_pipeline and self._embedding_provider:
            try:
                embedded = await self.bulk_embed_from_bm25s(batch_size=indexed_count)
                if embedded > 0:
                    logger.info("[DAEMON] Embedded %d documents after refresh", embedded)
            except Exception as e:
                logger.warning("Embedding after refresh failed: %s", e)

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

        # Read BM25S corpus (main + delta — new docs go into delta first)
        corpus = list(getattr(self._bm25s_index, "_corpus", []))
        paths = list(getattr(self._bm25s_index, "_paths", []))
        delta_corpus = getattr(self._bm25s_index, "_delta_corpus", [])
        delta_paths = getattr(self._bm25s_index, "_delta_paths", [])
        if delta_corpus:
            corpus.extend(delta_corpus)
            paths.extend(delta_paths)
        total = len(corpus)
        if total == 0:
            logger.info(
                "[BULK-EMBED] BM25S corpus is empty (main=%d, delta=%d)",
                len(getattr(self._bm25s_index, "_corpus", [])),
                len(delta_corpus),
            )
            return 0

        logger.info(
            f"[BULK-EMBED] Processing {total} documents from BM25S corpus"
            f" (main={len(getattr(self._bm25s_index, '_corpus', []))}"
            f", delta={len(delta_corpus)})"
        )

        # Step 1: Build deterministic UUID5 path_ids for each virtual path
        ns = uuid.UUID("12345678-1234-5678-1234-567812345678")
        path_id_map: dict[str, str] = {}
        now = datetime.utcnow()  # noqa: DTZ003 — column is timestamp without time zone

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
                            "INSERT INTO file_paths "
                            "(path_id, virtual_path, zone_id, backend_id, physical_path, "
                            " tenant_id, size_bytes, current_version, created_at, updated_at) "
                            "VALUES (:pid, :vpath, 'default', 'local', :vpath, "
                            " 'default', 0, 1, :now, :now) "
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
    # Index Maintenance (purge + rebuild)
    # =========================================================================

    async def purge_by_prefix(self, path_prefix: str) -> dict[str, int]:
        """Delete all search data matching a path prefix.

        Removes entries from document_chunks, file_paths, and BM25S index
        for any path starting with the given prefix. Production use cases:
        - Workspace/folder deletion cleanup
        - Stale test data removal
        - Zone teardown

        Args:
            path_prefix: Path prefix to match (e.g. "/test-search/" or "/workspace/123/")

        Returns:
            Dict with counts: chunks_deleted, paths_deleted, bm25s_deleted
        """
        from sqlalchemy import text

        result = {"chunks_deleted": 0, "paths_deleted": 0, "bm25s_deleted": 0}

        if not path_prefix or not self._async_session:
            return result

        # Also match zone-prefixed paths: /zone/*/prefix
        zone_pattern = f"/zone/%{path_prefix}%"
        plain_pattern = f"{path_prefix}%"

        async with self._async_session() as session:
            # Delete chunks first (FK constraint)
            r = await session.execute(
                text(
                    "DELETE FROM document_chunks WHERE path_id IN ("
                    "  SELECT path_id FROM file_paths"
                    "  WHERE virtual_path LIKE :plain OR virtual_path LIKE :zone"
                    ")"
                ),
                {"plain": plain_pattern, "zone": zone_pattern},
            )
            result["chunks_deleted"] = r.rowcount or 0

            # Delete file_paths
            r = await session.execute(
                text(
                    "DELETE FROM file_paths"
                    " WHERE virtual_path LIKE :plain OR virtual_path LIKE :zone"
                ),
                {"plain": plain_pattern, "zone": zone_pattern},
            )
            result["paths_deleted"] = r.rowcount or 0
            await session.commit()

        # Rebuild BM25S from DB (batch approach — avoids O(n²) per-item rebuild)
        if self._bm25s_index:
            old_stats = await self._bm25s_index.get_stats()
            old_count = old_stats.get("total_documents", 0)
            await self.rebuild_bm25s_from_db()
            new_stats = await self._bm25s_index.get_stats()
            new_count = new_stats.get("total_documents", 0)
            result["bm25s_deleted"] = max(0, old_count - new_count)

        logger.info(
            "[PURGE] prefix=%s: %d chunks, %d paths, %d bm25s entries removed",
            path_prefix,
            result["chunks_deleted"],
            result["paths_deleted"],
            result["bm25s_deleted"],
        )
        return result

    async def rebuild_bm25s_from_db(self) -> int:
        """Rebuild the BM25S index from document_chunks in PostgreSQL.

        Reads all chunk text from the database, groups by file, and
        re-indexes into BM25S. Use after purge, corruption, or when the
        on-disk index is lost. Production use cases:
        - Index recovery after disk loss
        - Resync BM25S with database after bulk operations
        - Post-migration reindex

        Returns:
            Number of documents indexed into BM25S
        """
        from sqlalchemy import text

        if not self._bm25s_index or not self._async_session:
            logger.warning("[REBUILD] BM25S or DB session not available")
            return 0

        # Clear existing BM25S data
        await self._bm25s_index.clear()

        # Read all documents from DB, grouped by file
        async with self._async_session() as session:
            rows = await session.execute(
                text(
                    "SELECT fp.virtual_path, fp.path_id,"
                    " string_agg(dc.chunk_text, E'\\n\\n' ORDER BY dc.chunk_index) as full_text"
                    " FROM file_paths fp"
                    " JOIN document_chunks dc ON dc.path_id = fp.path_id"
                    " WHERE fp.deleted_at IS NULL"
                    " GROUP BY fp.virtual_path, fp.path_id"
                )
            )
            docs = rows.fetchall()

        if not docs:
            logger.info("[REBUILD] No documents found in database")
            return 0

        # Bulk-index into BM25S
        indexed = 0
        for row in docs:
            vpath, path_id, full_text = row[0], row[1], row[2]
            if full_text:
                # Strip zone prefix for consistent BM25S paths
                clean_path = _strip_zone_prefix(vpath)
                await self._bm25s_index.index_document(path_id, clean_path, full_text)
                indexed += 1

        # Merge delta into main index and save
        await self._bm25s_index.rebuild_index()

        # Update stats
        self.stats.bm25_documents = indexed
        logger.info("[REBUILD] BM25S rebuilt from DB: %d documents indexed", indexed)
        return indexed

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
            # QMD pipeline configuration (config = intent, active = runtime state)
            "pipeline": {
                "query_expansion_enabled": self.config.query_expansion_enabled,
                "query_expansion_active": self._expansion_active,
                "expansion_provider": self.config.expansion_provider,
                "reranking_enabled": self.config.reranking_enabled,
                "reranking_active": self._reranking_active,
                "reranker_model": self.config.reranker_model,
                "position_aware_blending": self.config.position_aware_blending,
                "scored_chunking_enabled": self.config.scored_chunking_enabled,
            },
            "pipeline_stage_latencies": self.stats.pipeline_stage_latencies,
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


def _detect_reranker_defaults() -> tuple[str, str]:
    """Auto-detect reranker provider and model based on available API keys.

    Returns:
        (provider, model) tuple — prefers API keys if set, falls back to local.
    """
    import os

    if os.environ.get("JINA_API_KEY"):
        return "jina", "jina-reranker-v3"
    if os.environ.get("COHERE_API_KEY"):
        return "cohere", "cohere-rerank-v3.5"
    return "local", "jina-tiny"


def get_pipeline_config_from_env() -> dict[str, Any]:
    """Read QMD pipeline flags from environment variables.

    Environment variables:
        NEXUS_SEARCH_EMBEDDING_PROVIDER: "openai" | "fastembed" | "voyage" (default: openai)
        NEXUS_SEARCH_EMBEDDING_MODEL: Model name (default: text-embedding-3-small)
        NEXUS_SEARCH_EXPANSION_ENABLED: Enable query expansion (default: false)
        NEXUS_SEARCH_EXPANSION_PROVIDER: "openrouter" | "local" (default: openrouter)
        NEXUS_SEARCH_EXPANSION_MODEL: Model name or GGUF path
        NEXUS_SEARCH_RERANKING_ENABLED: Enable reranking (default: false)
        NEXUS_SEARCH_RERANKER_PROVIDER: "local" | "jina" | "cohere" (auto-detected)
        NEXUS_SEARCH_RERANKER_MODEL: Key from RERANKER_MODELS (auto-detected)
        NEXUS_SEARCH_RERANKING_TOP_K: Max candidates to rerank (default: 30)
        NEXUS_SEARCH_POSITION_BLENDING: Position-aware blending (default: true)
        NEXUS_SEARCH_SCORED_CHUNKING: Scored break-point chunking (default: false)

    Returns:
        Dict of pipeline config fields suitable for DaemonConfig(**pipeline_config).
    """
    import os

    def _bool(key: str, default: bool) -> bool:
        val = os.environ.get(key, "").lower()
        if val in ("true", "1", "yes"):
            return True
        if val in ("false", "0", "no"):
            return False
        return default

    def _int(key: str, default: int) -> int:
        val = os.environ.get(key)
        return int(val) if val else default

    # Default model depends on provider — fastembed needs a fastembed-compatible model
    _provider = os.environ.get("NEXUS_SEARCH_EMBEDDING_PROVIDER", "openai")
    _default_model = {
        "fastembed": "BAAI/bge-small-en-v1.5",
        "openai": "text-embedding-3-small",
        "voyage": "voyage-3",
        "voyage-lite": "voyage-3-lite",
    }.get(_provider, "text-embedding-3-small")

    # Auto-detect reranker provider/model from available API keys
    _default_reranker_provider, _default_reranker_model = _detect_reranker_defaults()

    return {
        "embedding_provider": _provider,
        "embedding_model": os.environ.get("NEXUS_SEARCH_EMBEDDING_MODEL", _default_model),
        "query_expansion_enabled": _bool("NEXUS_SEARCH_EXPANSION_ENABLED", False),
        "expansion_provider": os.environ.get("NEXUS_SEARCH_EXPANSION_PROVIDER", "openrouter"),
        "expansion_model": os.environ.get("NEXUS_SEARCH_EXPANSION_MODEL", "deepseek/deepseek-chat"),
        "reranking_enabled": _bool("NEXUS_SEARCH_RERANKING_ENABLED", False),
        "reranker_provider": os.environ.get(
            "NEXUS_SEARCH_RERANKER_PROVIDER", _default_reranker_provider
        ),
        "reranker_model": os.environ.get("NEXUS_SEARCH_RERANKER_MODEL", _default_reranker_model),
        "reranking_top_k": _int("NEXUS_SEARCH_RERANKING_TOP_K", 30),
        "position_aware_blending": _bool("NEXUS_SEARCH_POSITION_BLENDING", True),
        "scored_chunking_enabled": _bool("NEXUS_SEARCH_SCORED_CHUNKING", False),
    }
