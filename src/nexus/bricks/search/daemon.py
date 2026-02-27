"""Hot Search Daemon — txtai-backed unified search service (Issue #2663).

Delegates all search, indexing, and graph operations to a pluggable
:class:`SearchBackendProtocol` (default: ``TxtaiBackend``).

Features:
    - Hybrid BM25+dense search via txtai
    - Zone-level namespace isolation (zone_id on every call)
    - Optional async cross-encoder reranking
    - Opt-in auto-indexing with debounced background loop
    - Explicit call-by-call indexing API
    - Zoekt trigram search fallback for keyword queries

Performance targets:
    - First query: <50ms (pre-warmed)
    - P99 latency: <100ms

Issue: #951, #2663
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.txtai_backend import SearchBackendProtocol, create_backend
from nexus.lib.env import get_database_url

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================


def _strip_zone_prefix(path: str) -> str:
    """Strip ``/zone/{zone_id}/`` prefix from a path.

    API clients may send zone-scoped paths (e.g. ``/zone/corp/docs/file.py``),
    but NexusFS expects raw virtual paths (``/docs/file.py``).  The zone_id is
    passed separately as a keyword argument.

    Returns the original path unchanged if no zone prefix is present.
    """
    if path.startswith("/zone/"):
        # "/zone/{zone_id}/rest/of/path" → "/rest/of/path"
        parts = path[6:].split("/", 1)
        if len(parts) > 1:
            return f"/{parts[1]}"
        return "/"
    return path


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class DaemonStats:
    """Runtime statistics for the search daemon."""

    startup_time_ms: float = 0.0
    total_queries: int = 0
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    last_index_refresh: float | None = None
    zoekt_available: bool = False
    documents_indexed: int = 0


@dataclass
class SearchResult(BaseSearchResult):
    """Unified search result from daemon.

    Extends BaseSearchResult with search_type field.
    """

    search_type: str = "hybrid"


@dataclass
class DaemonConfig:
    """Configuration for the search daemon."""

    # Database
    database_url: str | None = None
    db_pool_min_size: int = 10
    db_pool_max_size: int = 50

    # txtai backend (Decision #1, #2)
    search_backend: str = "txtai"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    hybrid_search: bool = True

    # Indexing (Decision #18)
    auto_index_on_write: bool = False
    refresh_debounce_seconds: float = 5.0

    # Cross-encoder reranking (Decision #16)
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 25

    # Performance
    query_timeout_seconds: float = 10.0


# =============================================================================
# SearchDaemon
# =============================================================================


class SearchDaemon:
    """Long-running search service backed by txtai.

    Usage::

        daemon = SearchDaemon(config)
        await daemon.startup()
        results = await daemon.search("authentication", zone_id="corp", limit=10)
        await daemon.shutdown()
    """

    def __init__(
        self,
        config: DaemonConfig | None = None,
        *,
        async_session_factory: Any | None = None,
        zoekt_client: Any | None = None,
        cache_brick: Any | None = None,
    ) -> None:
        self.config = config or DaemonConfig()
        self.stats = DaemonStats()

        # Injected dependencies
        self._zoekt_client = zoekt_client
        self._cache_brick = cache_brick
        self._async_session = async_session_factory

        # Search backend
        self._backend: SearchBackendProtocol | None = None

        # Reranker (initialized on startup if enabled)
        self._reranker: Any | None = None

        # State
        self._initialized = False
        self._shutting_down = False

        # Auto-index (Decision #18)
        self._refresh_task: asyncio.Task[None] | None = None
        self._pending_index_docs: list[dict[str, Any]] = []
        self._index_lock = asyncio.Lock()

        # Latency tracking (circular buffer)
        self._latencies: list[float] = []
        self._max_latency_samples = 1000

        # Per-search timing breakdown (set after each search() call)
        self.last_search_timing: dict[str, float] = {}

        # FileReaderProtocol reference (set by FastAPI server)
        self._file_reader: Any = None
        # Adaptive-k provider (LEGO DI)
        self._adaptive_k_provider: Any = None
        # Embedding provider exposed for graph_search_service compat
        self._embedding_provider: Any = None

    @property
    def is_initialized(self) -> bool:
        """Check if daemon is fully initialized."""
        return self._initialized

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def startup(self) -> None:
        """Initialize backend, reranker, and auto-index loop."""
        if self._initialized:
            logger.warning("SearchDaemon already initialized")
            return

        start_time = time.perf_counter()
        logger.info("Starting SearchDaemon — initializing txtai backend...")

        # Create and start search backend
        self._backend = create_backend(
            self.config.search_backend,
            database_url=self.config.database_url,
            model=self.config.embedding_model,
            hybrid=self.config.hybrid_search,
        )
        await self._backend.startup()

        # Init configurable CE reranker (Decision #16)
        if self.config.reranker_enabled:
            self._init_reranker()

        # Check Zoekt availability
        await self._check_zoekt()

        # Start auto-index loop only if opt-in (Decision #18)
        if self.config.auto_index_on_write:
            self._refresh_task = asyncio.create_task(self._auto_index_loop())

        self._initialized = True
        self.stats.startup_time_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            "SearchDaemon ready in %.1fms (backend=%s, reranker=%s)",
            self.stats.startup_time_ms,
            self.config.search_backend,
            self.config.reranker_enabled,
        )

    async def shutdown(self) -> None:
        """Gracefully shutdown the daemon."""
        if self._shutting_down:
            return

        self._shutting_down = True
        logger.info("Shutting down SearchDaemon...")

        # Cancel auto-index task
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task

        # Shutdown backend
        if self._backend:
            await self._backend.shutdown()

        self._initialized = False
        logger.info("SearchDaemon shutdown complete")

    # =========================================================================
    # Search
    # =========================================================================

    async def search(
        self,
        query: str,
        search_type: Literal["keyword", "semantic", "hybrid"] = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,  # noqa: ARG002
        fusion_method: str = "rrf",  # noqa: ARG002
        adaptive_k: bool = False,
        zone_id: str | None = None,
        rerank: bool | None = None,
    ) -> list[SearchResult]:
        """Execute a search query with mandatory zone_id isolation.

        The brick handles ONLY zone-level isolation (WHERE zone_id = :zone_id).
        File-level ReBAC permission filtering is NOT done here — it belongs
        in the server/router layer to avoid brick-to-service coupling.

        Args:
            query: Search query text
            search_type: "keyword", "semantic", or "hybrid"
            limit: Maximum results
            path_filter: Optional path prefix filter
            alpha: Unused (kept for API compat; txtai handles fusion internally)
            fusion_method: Unused (txtai handles fusion internally)
            adaptive_k: If True, dynamically adjust limit
            zone_id: Namespace for search isolation (required for multi-tenant)
            rerank: Override reranker enabled/disabled

        Returns:
            List of SearchResult sorted by relevance
        """
        if not self._initialized:
            msg = "SearchDaemon not initialized. Call startup() first."
            raise RuntimeError(msg)

        if not self._backend:
            return []

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID

        # Adaptive-k (LEGO DI)
        if adaptive_k and self._adaptive_k_provider is not None:
            original_limit = limit
            limit = self._adaptive_k_provider.calculate_k_dynamic(query, k_base=limit)
            if limit != original_limit:
                logger.info(
                    "[SEARCH-DAEMON] Adaptive k: %d -> %d for query: %s",
                    original_limit,
                    limit,
                    query[:50],
                )

        start = time.perf_counter()
        backend_ms = 0.0
        rerank_ms = 0.0

        # Try Zoekt first for keyword queries
        if search_type == "keyword" and self.stats.zoekt_available:
            backend_start = time.perf_counter()
            zoekt_results = await self._search_zoekt(query, limit, path_filter)
            backend_ms = (time.perf_counter() - backend_start) * 1000
            if zoekt_results:
                latency_ms = (time.perf_counter() - start) * 1000
                self._track_latency(latency_ms)
                self.last_search_timing = {
                    "backend_ms": round(backend_ms, 2),
                    "rerank_ms": 0.0,
                }
                return zoekt_results

        # Delegate to txtai backend
        backend_start = time.perf_counter()
        raw_results = await self._backend.search(
            query,
            zone_id=effective_zone_id,
            limit=limit,
            search_type=search_type,
            path_filter=path_filter,
        )
        backend_ms = (time.perf_counter() - backend_start) * 1000

        # Convert to SearchResult
        results = [
            SearchResult(
                path=r.path,
                chunk_text=r.chunk_text,
                score=r.score,
                chunk_index=r.chunk_index,
                start_offset=r.start_offset,
                end_offset=r.end_offset,
                line_start=r.line_start,
                line_end=r.line_end,
                keyword_score=r.keyword_score,
                vector_score=r.vector_score,
                reranker_score=r.reranker_score,
                search_type=search_type,
            )
            for r in raw_results
        ]

        # Async CE reranking (Decision #16)
        if self._should_rerank(rerank):
            rerank_start = time.perf_counter()
            results = await self._rerank_async(query, results)
            rerank_ms = (time.perf_counter() - rerank_start) * 1000

        latency_ms = (time.perf_counter() - start) * 1000
        self._track_latency(latency_ms)

        self.last_search_timing = {
            "backend_ms": round(backend_ms, 2),
            "rerank_ms": round(rerank_ms, 2),
        }

        return results

    # =========================================================================
    # Indexing API (Decision #18)
    # =========================================================================

    async def index_documents(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Explicitly index documents.

        Each doc: ``{"id": str, "text": str, "path": str, ...}``
        """
        if not self._backend:
            return 0

        count = await self._backend.upsert(documents, zone_id=zone_id)
        self.stats.documents_indexed += count
        return count

    async def delete_documents(self, ids: list[str], *, zone_id: str) -> int:
        """Delete documents by id."""
        if not self._backend:
            return 0
        return await self._backend.delete(ids, zone_id=zone_id)

    async def notify_file_change(
        self,
        path: str,
        change_type: str = "update",
        *,
        zone_id: str = "",
    ) -> None:
        """Handle a file change notification for index refresh.

        When called explicitly (e.g. from the /refresh endpoint), this reads
        the file content via ``_file_reader`` and upserts it into the backend
        immediately — regardless of ``auto_index_on_write``.

        For auto-indexing (background debounced loop), this queues the document
        only when ``auto_index_on_write=True``.

        Args:
            path: Virtual path of the changed file (may include /zone/{id}/ prefix).
            change_type: One of "create", "update", "delete".
            zone_id: Zone namespace for isolation.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID

        # Strip /zone/{zone_id}/ prefix — NexusFS expects raw paths,
        # but API clients may send zone-scoped paths.
        read_path = _strip_zone_prefix(path)
        # Use the stripped path for both storage and reading
        store_path = read_path

        # Handle deletions
        if change_type == "delete":
            await self.delete_documents([store_path], zone_id=effective_zone_id)
            return

        # Read content via FileReaderProtocol when available
        content = ""
        if self._file_reader is not None:
            try:
                content = await asyncio.to_thread(self._file_reader.read_text, read_path)
            except Exception:
                logger.warning("Failed to read file for indexing: %s", read_path, exc_info=True)
                return

        if not content:
            logger.debug("No content for %s — skipping index", read_path)
            return

        doc = {"id": store_path, "text": content, "path": store_path}

        # Explicit upsert into backend (works regardless of auto_index_on_write)
        if self._backend:
            count = await self._backend.upsert([doc], zone_id=effective_zone_id)
            self.stats.documents_indexed += count
            logger.info(
                "Indexed file %s in zone %s (change_type=%s)",
                store_path,
                effective_zone_id,
                change_type,
            )

    async def _auto_index_loop(self) -> None:
        """Debounced background loop for auto-indexing queued documents."""
        while not self._shutting_down:
            await asyncio.sleep(self.config.refresh_debounce_seconds)
            async with self._index_lock:
                if not self._pending_index_docs:
                    continue
                batch = list(self._pending_index_docs)
                self._pending_index_docs.clear()
            try:
                by_zone: dict[str, list[dict[str, Any]]] = {}
                for doc in batch:
                    by_zone.setdefault(doc.get("zone_id", ""), []).append(doc)
                for zid, docs in by_zone.items():
                    if self._backend:
                        await self._backend.upsert(docs, zone_id=zid)
                        self.stats.documents_indexed += len(docs)
                logger.info("Auto-indexed %d documents across %d zones", len(batch), len(by_zone))
            except Exception:
                logger.exception("Auto-index batch failed")

    # =========================================================================
    # Reranking (Decision #16)
    # =========================================================================

    def _init_reranker(self) -> None:
        try:
            from txtai.pipeline import Similarity

            self._reranker = Similarity(self.config.reranker_model, crossencode=True)
            logger.info("Cross-encoder reranker loaded: %s", self.config.reranker_model)
        except Exception:
            logger.warning("Failed to load reranker, disabling", exc_info=True)
            self._reranker = None

    def _should_rerank(self, rerank_override: bool | None) -> bool:
        if rerank_override is not None:
            return rerank_override
        return self._reranker is not None

    async def _rerank_async(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        """Cross-encoder reranking in a thread pool."""
        if not results:
            return results

        top_k = self.config.reranker_top_k
        texts = [r.chunk_text for r in results[:top_k]]

        assert self._reranker is not None
        scores = await asyncio.to_thread(self._reranker, query, texts)

        for idx, score in scores:
            if idx < len(results):
                results[idx].reranker_score = float(score)

        results.sort(key=lambda r: r.reranker_score or r.score, reverse=True)
        return results

    # =========================================================================
    # Zoekt (keyword fallback, orthogonal to txtai)
    # =========================================================================

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
            logger.debug("Zoekt search failed: %s", e)
            return []

    # =========================================================================
    # Statistics
    # =========================================================================

    def _track_latency(self, latency_ms: float) -> None:
        """Track query latency for statistics."""
        self._latencies.append(latency_ms)
        if len(self._latencies) > self._max_latency_samples:
            self._latencies.pop(0)

        self.stats.total_queries += 1

        if self._latencies:
            self.stats.avg_latency_ms = sum(self._latencies) / len(self._latencies)
            sorted_latencies = sorted(self._latencies)
            p99_idx = int(len(sorted_latencies) * 0.99)
            self.stats.p99_latency_ms = sorted_latencies[p99_idx] if sorted_latencies else 0

    def get_stats(self) -> dict[str, Any]:
        """Get current daemon statistics."""
        return {
            "initialized": self._initialized,
            "startup_time_ms": self.stats.startup_time_ms,
            "total_queries": self.stats.total_queries,
            "avg_latency_ms": round(self.stats.avg_latency_ms, 2),
            "p99_latency_ms": round(self.stats.p99_latency_ms, 2),
            "last_index_refresh": self.stats.last_index_refresh,
            "zoekt_available": self.stats.zoekt_available,
            "documents_indexed": self.stats.documents_indexed,
            "bm25_documents": self.stats.documents_indexed,
            "embedding_cache_connected": self._cache_brick is not None,
            "backend": self.config.search_backend,
            "reranker_enabled": self.config.reranker_enabled,
        }

    def get_health(self) -> dict[str, Any]:
        """Get health status for health check endpoint."""
        return {
            "status": "healthy" if self._initialized else "starting",
            "daemon_initialized": self._initialized,
            "backend_ready": self._backend is not None,
            "bm25_index_loaded": self._initialized and self._backend is not None,
            "db_pool_ready": self._backend is not None,
            "zoekt_available": self.stats.zoekt_available,
        }


# =============================================================================
# Convenience factory
# =============================================================================


async def create_and_start_daemon(
    database_url: str | None = None,
    *,
    async_session_factory: Any | None = None,
) -> SearchDaemon:
    """Create, configure and start a search daemon.

    Args:
        database_url: Database URL (from env if not provided)
        async_session_factory: Injected async_sessionmaker from RecordStoreABC

    Returns:
        Initialized SearchDaemon instance
    """
    config = DaemonConfig(
        database_url=database_url or get_database_url(),
    )
    daemon = SearchDaemon(config, async_session_factory=async_session_factory)
    await daemon.startup()
    return daemon
