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
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text as sa_text

from nexus.bricks.search.chunk_store import ChunkRecord, ChunkStore
from nexus.bricks.search.mutation_events import (
    SearchMutationEvent,
    SearchMutationOp,
    extract_zone_id,
    strip_zone_prefix,
)
from nexus.bricks.search.mutation_resolver import MutationResolver, ResolvedMutation
from nexus.bricks.search.results import BaseSearchResult
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.env import get_database_url

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from nexus.bricks.search.chunking import EntropyAwareChunker
    from nexus.bricks.search.index_scope import IndexScope
    from nexus.bricks.search.indexing import IndexingPipeline
    from nexus.bricks.search.path_context import PathContextCache

logger = logging.getLogger(__name__)


@dataclass
class DaemonStats:
    """Runtime statistics for the search daemon."""

    startup_time_ms: float = 0.0
    db_pool_size: int = 0
    db_pool_warmup_time_ms: float = 0.0
    vector_warmup_time_ms: float = 0.0
    total_queries: int = 0
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    last_index_refresh: float | None = None
    zoekt_available: bool = False
    embedding_cache_connected: bool = False
    mutation_consumers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Fail-soft counters for path-context attach (Issue #3773): search must
    # never 500 on a context-lookup bug, but persistent failures should be
    # visible via /search/stats rather than only in log lines.
    path_context_attach_failures: int = 0
    path_context_resolve_failures: int = 0


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

    # Vector search settings
    vector_warmup_enabled: bool = True
    vector_ef_search: int = 100  # HNSW recall parameter

    # Index refresh settings
    refresh_debounce_seconds: float = 5.0
    refresh_enabled: bool = True
    mutation_poll_seconds: float = 2.0
    mutation_batch_size: int = 100

    # Performance settings
    query_timeout_seconds: float = 10.0
    max_indexing_concurrency: int = 10  # Issue #2071: from ProfileTuning.search

    # Entropy-aware filtering (Issue #1024)
    entropy_filtering: bool = False
    entropy_threshold: float = 0.35  # SimpleMem's τ_redundant
    entropy_alpha: float = 0.5  # Balance entity vs semantic novelty

    # Legacy txtai-era config fields kept on the dataclass for backward
    # compatibility with code paths that still pass them as kwargs (the
    # FastAPI lifespan in particular). Issue #3699 dropped the txtai
    # backend, so the default for ``txtai_model`` is now ``None`` —
    # the previous "sentence-transformers/all-MiniLM-L6-v2" default
    # was 384-dim while EmbeddingClient defaults to 1536-dim, which
    # would crash pgvector if accidentally consumed. Operators must
    # set ``embedding_model`` (or supply a compatible ``txtai_model``
    # via lifespan) explicitly.
    txtai_model: str | None = None
    txtai_vectors: dict[str, Any] | None = None
    txtai_reranker: str | None = None
    txtai_sparse: bool = False
    txtai_graph: bool = False
    # Modern names (preferred). Default model is ``None`` so unconfigured
    # deploys stay in keyword-only mode.
    embedding_model: str | None = None
    embedding_dimensions: int | None = 1536

    # Page-level aggregation for chunked retrieval (Issue #3980).
    # Per-chunk BM25 scoring dilutes rare-phrase signal; for those queries
    # the literal-match chunk's signal gets diluted across the page's other
    # chunks (e.g. a "40 Under 40" mention buried at chunk-rank 33 instead of
    # top-5). Aggregating chunk scores up to page level via max-pool fixes
    # this — it's the same pattern as Vespa/ColBERT MaxSim and gbrain's
    # "Best-of-Page" dedup. Default on; set NEXUS_SEARCH_PAGE_AGGREGATION=false
    # to disable for ablation.
    page_aggregation: bool = True
    chunks_per_page: int = 2  # gbrain emission cap; protects against one-doc dominance
    # Page-level BM25 leg (Issue #3980 follow-up). Per-term FTS lookups against
    # the full-document text, RRF-fused, then folded into the chunk-aggregated
    # ranking. Recovers rare-phrase docs that lose at chunk granularity because
    # tsquery AND-zeros pages missing any single query term. Default on; set
    # NEXUS_SEARCH_PAGE_BM25=false to disable.
    page_bm25: bool = True
    page_bm25_rrf_k: int = 60

    # Per-directory semantic index scoping (Issue #3698).
    # ``scope_refresh_seconds`` controls how often the daemon re-reads
    # ``zones.indexing_mode`` and ``indexed_directories`` from the DB
    # so multi-worker deployments converge on a consistent view of the
    # scope state. The cost is one small SQL query per refresh
    # interval per worker. Single-process deployments can leave this
    # at the default (5s) — the daemon also write-throughs on the
    # CRUD endpoints in the local process, so the periodic refresh
    # only matters when ANOTHER worker mutated scope.
    scope_refresh_seconds: float = 5.0

    # Path-context cache bound (Issue #3773 review). Multi-tenant deployments
    # with thousands of active zones can outgrow the default LRU cap; expose
    # as config so operators can tune without a code change.
    path_context_max_zones: int = 2048


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
        settings_store: Any | None = None,
        path_context_cache: "PathContextCache | None" = None,
        sqlite_vec_backend: Any | None = None,
    ):
        """Initialize the search daemon.

        Args:
            config: Daemon configuration (uses defaults if not provided)
            async_session_factory: Injected async_sessionmaker from RecordStoreABC.
                When provided, skips creating a private engine (Issue #1597).
            zoekt_client: Injected ZoektClient instance (Issue #2188).
            cache_brick: Injected CacheBrick for embedding cache health checks.
            settings_store: Optional SystemSettingsStoreProtocol for durable
                consumer checkpoints.
            path_context_cache: Optional PathContextCache for attaching admin-
                configured path descriptions onto every ``SearchResult``
                returned by :meth:`search` (Issue #3773).
        """
        self.config = config or DaemonConfig()
        self.stats = DaemonStats()
        self._zoekt_client = zoekt_client
        self._cache_brick = cache_brick
        self._settings_store = settings_store
        self._path_context_cache = path_context_cache
        # Codex review R6 (high): SANDBOX local sqlite-vec backend so
        # daemon-driven indexing (mutation events → IndexingPipeline)
        # populates the hybrid vector lane. Without this, only the
        # SearchService.initialize_semantic_search RPC path mirrors
        # writes; the production daemon refresh loop would silently
        # leave the vec lane empty.
        self._sqlite_vec_backend = sqlite_vec_backend
        self._path_context_cache_by_loop: dict[Any, Any] = {}
        # Engines we created for loop-local caches — tracked for disposal
        # on shutdown so pooled connections don't leak (Issue #3773 review).
        self._path_context_engines_by_loop: dict[Any, Any] = {}

        # Search components (initialized on startup)
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
        self._legacy_refresh_task: asyncio.Task | None = None
        self._mutation_wakeup = asyncio.Event()
        self._pending_refresh_paths: set[str] = set()
        self._pending_delete_paths: set[str] = set()
        self._refresh_lock = asyncio.Lock()
        self._mutation_resolver: MutationResolver | None = None
        self._chunk_store: ChunkStore | None = None
        self._consumer_tasks: dict[str, asyncio.Task[None]] = {}
        self._consumer_names: tuple[str, ...] = ()
        self._consumer_failures: dict[str, int] = {}
        self._consumer_last_error: dict[str, str | None] = {}
        self._consumer_last_sequence: dict[str, int] = {}
        self._checkpoint_file = Path(".nexus-data") / "mutation-checkpoints.json"
        self._checkpoint_lock = asyncio.Lock()
        self._shared_mutation_lock = asyncio.Lock()
        self._shared_mutation_events: list[SearchMutationEvent] = []
        self._shared_mutation_floor_sequence = 0
        self._shared_mutation_loaded_at = 0.0

        # FileReaderProtocol reference for reading file content (set by FastAPI server)
        # Issue #1520: Replaces direct NexusFS dependency
        self._file_reader: Any = None

        # SPLADE learned sparse retrieval (optional, initialized in startup)
        self._splade: Any = None

        # Search backends (Issue #3699: replaced txtai). The daemon picks
        # PgFtsBackend + PgVectorBackend for postgresql URLs and
        # SqliteFtsBackend + SqliteVecBackend otherwise. Each backend is
        # storage-only; query embedding is shared via ``_embedding_client``.
        self._fts_backend: Any = None
        self._vector_backend: Any = None
        self._embedding_client: Any = None
        self.last_search_timing: dict[str, float] = {}

        # Skeleton index (Issue #3725) — in-memory BM25-lite for /locate endpoint.
        # Bootstrapped from document_skeleton DB rows; no file reads on restart (13B).
        # Keys: virtual_path; values: {path_id, zone_id, title, path_tokens}.
        self._skeleton_docs: dict[str, dict[str, Any]] = {}
        self._skeleton_bootstrap_task: asyncio.Task[None] | None = None
        self._skeleton_bootstrapped: bool = False

        # Latency tracking (circular buffer)
        self._latencies: list[float] = []
        self._max_latency_samples = 1000

        # Per-directory semantic index scope state (Issue #3698).
        # The local-process CRUD endpoints update these under
        # ``_refresh_lock`` as a write-through cache on top of the
        # ``zones.indexing_mode`` column and the ``indexed_directories``
        # table. A periodic refresh task (``_scope_refresh_loop``)
        # re-reads from the DB so multi-worker deployments converge on
        # a consistent view of scope state — see
        # ``DaemonConfig.scope_refresh_seconds``.
        self._zone_indexing_modes: dict[str, str] = {}
        self._indexed_directories: dict[str, set[str]] = {}
        # Monotonic counter incremented on every scope mutation (local
        # CRUD or refresh tick that detects external changes).
        # Long-running backfills capture this at start and bail if it
        # advances mid-flight, so they cannot resurrect documents that
        # another worker just de-scoped.
        self._scope_generation: int = 0
        self._scope_refresh_task: asyncio.Task[None] | None = None

    @property
    def is_initialized(self) -> bool:
        """Check if daemon is fully initialized."""
        return self._initialized

    # =========================================================================
    # Index scope (Issue #3698) — per-directory semantic index scoping
    # =========================================================================

    def _current_index_scope(self) -> "IndexScope | None":
        """Return an immutable snapshot of the current per-zone index scope.

        Called by ``IndexingPipeline.index_documents`` on every batch to
        decide which paths reach the embedding provider. Cheap — builds a
        fresh frozen snapshot from the in-memory dicts under the assumption
        that CRUD endpoints hold ``_refresh_lock`` while writing.
        """
        from nexus.bricks.search.index_scope import IndexScope

        return IndexScope(
            zone_modes=dict(self._zone_indexing_modes),
            zone_directories={
                zone: frozenset(dirs) for zone, dirs in self._indexed_directories.items()
            },
        )

    async def _load_index_scope(self) -> None:
        """Populate ``_zone_indexing_modes`` and ``_indexed_directories``
        from the database at startup.

        Runs once from ``startup()`` before the search backends are
        initialised and before the mutation consumers spin up so the very
        first refresh already respects scope.

        **Fail-closed contract:** on any error fetching scope metadata
        we raise ``IndexScopeLoadError``, which propagates out of
        ``startup()`` and crashes the daemon. The alternative —
        defaulting every zone to ``'all'`` on error — silently disables
        scoped-mode enforcement, letting the daemon embed and serve
        data it was explicitly configured NOT to index. That is a
        trust-boundary failure; fail-fast is the only safe choice.
        Operators can restart after fixing the DB; the migration is
        required for startup to succeed.

        Silent no-op only when no async session is available at all
        (embedded test scaffolding).
        """
        if self._async_session is None:
            logger.debug("No async session available, skipping index scope load")
            return

        try:
            async with self._async_session() as session:
                # Load zone indexing modes. Zones with mode NULL (pre-migration
                # or test fixtures) default to 'all' per the backward-compat
                # rule in is_path_indexed.
                result = await session.execute(
                    sa_text(
                        "SELECT zone_id, COALESCE(indexing_mode, 'all') "
                        "FROM zones WHERE deleted_at IS NULL"
                    )
                )
                self._zone_indexing_modes = {row[0]: row[1] for row in result.fetchall()}

                # Load indexed directories grouped by zone.
                result = await session.execute(
                    sa_text("SELECT zone_id, directory_path FROM indexed_directories")
                )
                dirs_by_zone: dict[str, set[str]] = {}
                for row in result.fetchall():
                    dirs_by_zone.setdefault(row[0], set()).add(row[1])
                self._indexed_directories = dirs_by_zone

            # Bump generation so any in-flight backfill captured before
            # startup completes can detect the change and bail.
            self._scope_generation += 1
        except Exception as exc:
            # Reset state so a partial read can't leave the daemon in a
            # confusing half-loaded state if someone catches this upstream.
            self._zone_indexing_modes = {}
            self._indexed_directories = {}
            logger.error(
                "FATAL: failed to load index scope metadata; "
                "refusing to start in a degraded state where scoped "
                "zones would silently become 'all' and embed out-of-"
                "scope data. Fix the database and restart.",
                exc_info=True,
            )
            from nexus.bricks.search.index_scope import IndexScopeLoadError

            raise IndexScopeLoadError("Failed to load per-zone index scope from database") from exc

        logger.info(
            "Loaded index scope: %d zones (%d scoped), %d directories",
            len(self._zone_indexing_modes),
            sum(1 for m in self._zone_indexing_modes.values() if m == "scoped"),
            sum(len(d) for d in self._indexed_directories.values()),
        )

    async def _scope_refresh_loop(self) -> None:
        """Periodically re-read scope state from the DB.

        Multi-worker deployments need this so a CRUD endpoint call on
        worker A becomes visible to worker B within bounded staleness.
        Single-worker deployments tolerate it cheaply because the
        local write-through path keeps state fresh, and the periodic
        re-read costs one small SELECT every ``scope_refresh_seconds``.

        Uses ``_refresh_lock`` so the in-memory dicts don't see a half-
        loaded state if a CRUD call is mid-write.

        Distinct from ``_load_index_scope``: this path is best-effort —
        on transient DB failure it logs and retries on the next tick
        rather than crashing the daemon. The startup load is still the
        fail-closed path that establishes the initial trusted state.

        **Self-healing on shrink**: when the refresh detects that scope
        SHRANK (a directory disappeared OR a zone flipped from 'all'
        to 'scoped') it runs ``purge_unscoped_embeddings`` for the
        affected zones. This bounds the leak window from any
        cross-worker write that landed before the local view caught up
        to ``scope_refresh_seconds`` instead of "forever until manual
        cleanup".
        """
        interval = max(0.5, self.config.scope_refresh_seconds)
        while not self._shutting_down:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._shutting_down or self._async_session is None:
                return
            try:
                async with self._async_session() as session:
                    modes_result = await session.execute(
                        sa_text(
                            "SELECT zone_id, COALESCE(indexing_mode, 'all') "
                            "FROM zones WHERE deleted_at IS NULL"
                        )
                    )
                    new_modes = {row[0]: row[1] for row in modes_result.fetchall()}

                    dirs_result = await session.execute(
                        sa_text("SELECT zone_id, directory_path FROM indexed_directories")
                    )
                    new_dirs: dict[str, set[str]] = {}
                    for row in dirs_result.fetchall():
                        new_dirs.setdefault(row[0], set()).add(row[1])

                shrunk_zones: set[str] = set()
                async with self._refresh_lock:
                    old_modes = self._zone_indexing_modes
                    old_dirs = self._indexed_directories
                    # Detect "shrink" per zone: any of
                    #   (a) flipped 'all' → 'scoped'
                    #   (b) had directories removed (set difference non-empty)
                    # triggers a self-healing purge for that zone.
                    for zid in set(old_modes) | set(new_modes):
                        old_mode = old_modes.get(zid, "all")
                        new_mode = new_modes.get(zid, "all")
                        if old_mode == "all" and new_mode == "scoped":
                            shrunk_zones.add(zid)
                            continue
                        if new_mode == "scoped":
                            old_set = old_dirs.get(zid, set())
                            new_set = new_dirs.get(zid, set())
                            if old_set - new_set:
                                shrunk_zones.add(zid)

                    state_changed = new_modes != old_modes or new_dirs != old_dirs
                    self._zone_indexing_modes = new_modes
                    self._indexed_directories = new_dirs
                    if state_changed:
                        self._scope_generation += 1

                # Run purge OUTSIDE the refresh lock so it doesn't block
                # other refreshes. Best-effort: failures are logged.
                for zid in sorted(shrunk_zones):
                    try:
                        purged = await self.purge_unscoped_embeddings(zid)
                        # ``vector_docs`` is the canonical key after Issue #3699;
                        # ``txtai_docs`` is preserved as a deprecated alias for
                        # older clients reading the response shape directly.
                        purged_count = purged.get("vector_docs", 0) or purged.get("txtai_docs", 0)
                        if purged_count:
                            logger.info(
                                "scope refresh tick: zone %s shrank — "
                                "self-healed by purging %d stale vector docs",
                                zid,
                                purged_count,
                            )
                    except Exception:
                        logger.warning(
                            "scope refresh tick: self-healing purge failed "
                            "for zone %s; stale rows may remain until next tick",
                            zid,
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                return
            except Exception:
                # Best-effort: keep the old snapshot, log and retry next tick.
                logger.warning(
                    "scope refresh tick failed; keeping previous snapshot",
                    exc_info=True,
                )

    async def add_indexed_directory(self, zone_id: str, directory_path: str) -> Any:
        """Register ``directory_path`` for scoped indexing. See scope_ops.

        Returns ``(canonical_path, BackfillResult)``.
        """
        from nexus.bricks.search import scope_ops

        return await scope_ops.add_indexed_directory(self, zone_id, directory_path)

    async def rerun_backfill_for_directory(self, zone_id: str, directory_path: str) -> Any:
        """Re-trigger backfill for an already-registered directory.

        Used by the registration recovery path so an operator who hit a
        backfill failure can retry without unregister + re-register.
        """
        from nexus.bricks.search import scope_ops

        return await scope_ops.rerun_backfill_for_directory(self, zone_id, directory_path)

    async def remove_indexed_directory(self, zone_id: str, directory_path: str) -> str:
        """Unregister ``directory_path`` from scoped indexing. See scope_ops."""
        from nexus.bricks.search import scope_ops

        return await scope_ops.remove_indexed_directory(self, zone_id, directory_path)

    def list_indexed_directories(self, zone_id: str) -> list[str]:
        """List registered directories for ``zone_id``. See scope_ops."""
        from nexus.bricks.search import scope_ops

        return scope_ops.list_indexed_directories(self, zone_id)

    async def purge_unscoped_embeddings(self, zone_id: str | None = None) -> dict[str, int]:
        """Delete stored embeddings for out-of-scope files. See scope_ops."""
        from nexus.bricks.search import scope_ops

        return await scope_ops.purge_unscoped_embeddings(self, zone_id)

    def _is_path_in_scope(self, path: str) -> bool:
        """Optimization gate used by the daemon's refresh & consumer loops.

        Returns True if ``path`` (either a raw ``/zone/{id}/...`` string
        or a canonical virtual path) should be processed by the embedding
        pipeline, according to the current in-memory scope snapshot.

        This is a *duplicate* of the central gate in
        ``IndexingPipeline.index_documents`` — the central gate is the
        correctness boundary, this helper is the optimization that lets
        the daemon skip file I/O and DB round trips for out-of-scope
        paths. A quiet ``True`` is returned on any contract violation so
        the central gate can surface the error instead of silently losing
        the path here.
        """
        from nexus.bricks.search.index_scope import is_path_indexed

        scope = self._current_index_scope()
        if scope is None:
            return True
        try:
            zone_id = extract_zone_id(path)
            virtual_path = strip_zone_prefix(path)
            return is_path_indexed(scope, zone_id, virtual_path)
        except ValueError:
            # Let the central gate handle contract violations — this
            # helper is a fast-path optimization only.
            return True

    async def set_zone_indexing_mode(self, zone_id: str, mode: str) -> Any:
        """Flip zone between ``'all'`` and ``'scoped'``. See scope_ops.

        Returns ``BackfillResult | None`` — ``None`` when no backfill ran.
        """
        from nexus.bricks.search import scope_ops

        return await scope_ops.set_zone_indexing_mode(self, zone_id, mode)

    def _build_backends(self, database_url: str) -> tuple[Any, Any]:
        """Pick (fts_backend, vector_backend) by profile (Issue #3699).

        * Postgres URL → ``PgFtsBackend`` + ``PgVectorBackend`` reading the
          existing pg_textsearch BM25 index and pgvector halfvec(1536) HNSW
          index on ``document_chunks``.
        * Anything else (SQLite) → ``SqliteFtsBackend`` (FTS5 native bm25)
          + ``SqliteVecBackend`` (sqlite-vec extension, KNN over float32 packed
          embeddings).

        Imports are deferred so the daemon module can be imported on
        environments missing the optional sqlite-vec / litellm wheel until
        the SANDBOX path is actually exercised.
        """
        if "postgresql" in database_url:
            from nexus.bricks.search.pg_fts_backend import PgFtsBackend
            from nexus.bricks.search.pg_vector_backend import PgVectorBackend

            if self._async_engine is None:
                raise RuntimeError(
                    "_build_backends: postgres profile requires an initialised "
                    "_async_engine; got None"
                )
            engine = self._async_engine
            return (
                PgFtsBackend(engine=engine, chunk_store=self._chunk_store),
                PgVectorBackend(engine=engine, chunk_store=self._chunk_store),
            )

        from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend
        from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend

        sqlite_path = self._sqlite_path_from_url(database_url)
        return (
            SqliteFtsBackend(db_path=sqlite_path, chunk_store=self._chunk_store),
            SqliteVecBackend(db_path=sqlite_path),
        )

    @staticmethod
    def _sqlite_path_from_url(database_url: str) -> str:
        """Extract the on-disk path from a sqlite:/// or sqlite+aiosqlite:/// URL."""
        prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
        for prefix in prefixes:
            if database_url.startswith(prefix):
                return database_url[len(prefix) :]
        # Fall back to "":memory:" when nothing matches; SqliteVecBackend
        # will fail loudly if the path is unusable.
        return database_url or ":memory:"

    async def startup(self) -> None:
        """Initialize and pre-warm all search indexes.

        Idempotent — safe to call multiple times (e.g., from both
        startup_search and mount_all via lifecycle manager). Also safe
        to call after shutdown() for remount cycles.
        """
        if self._initialized:
            logger.warning("SearchDaemon already initialized")
            return

        # Reset shutdown flag for remount cycles (unmount → remount)
        self._shutting_down = False

        start_time = time.perf_counter()
        logger.info("Starting SearchDaemon - pre-warming indexes...")

        # Initialize database pool
        await self._init_database_pool()

        # Vector warmup needs DB pool to be ready
        if self.config.vector_warmup_enabled and self._async_engine:
            await self._warm_vector_index()

        # Check optional components
        await self._check_zoekt()
        await self._check_embedding_cache()

        # Load per-directory semantic index scope (Issue #3698) BEFORE
        # initializing the search backends. The scope snapshot governs
        # whether the embedding pipeline writes for a given path; loading
        # it first guarantees the very first refresh tick respects scope.
        # Must run synchronously — do NOT kick off as a background task.
        await self._load_index_scope()

        # Initialize search backends by profile (Issue #3699). The daemon
        # picks PgFtsBackend + PgVectorBackend for Postgres deployments and
        # SqliteFtsBackend + SqliteVecBackend on SQLite. Each backend is
        # storage-only — query embedding is owned by ``_embedding_client``.
        try:
            url = self.config.database_url or ""
            self._fts_backend, self._vector_backend = self._build_backends(url)
            await self._fts_backend.startup()
            await self._vector_backend.startup()
            logger.info(
                "search backends ready: fts=%s vector=%s",
                type(self._fts_backend).__name__,
                type(self._vector_backend).__name__,
            )
        except Exception:
            logger.warning(
                "search backend init failed; keyword/FTS fallback will still work",
                exc_info=True,
            )
            self._fts_backend = None
            self._vector_backend = None

        # Embedding client for query-time vectors. Also wired as the
        # ``_embedding_provider`` so the durable mutation consumer +
        # IndexingPipeline can run their batched calls (which use
        # ``embed_texts_batched``). Without this assignment the embedding
        # consumer no-ops on ``self._embedding_provider is None`` and
        # auto-index-on-edit silently drops every update — Issue #3699.
        try:
            from nexus.bricks.search.embedding_client import EmbeddingClient

            embedding_model = getattr(self.config, "embedding_model", None) or getattr(
                self.config, "txtai_model", None
            )
            # Default to text-embedding-3-small when an OpenAI key is in
            # env but the model name was not pinned. Without a default,
            # operators who set OPENAI_API_KEY but forget the model name
            # silently get keyword-only auto-indexing — naive FTS chunks
            # with no embeddings. Issue #3699.
            if not embedding_model and os.environ.get("OPENAI_API_KEY"):
                embedding_model = "text-embedding-3-small"
                logger.info(
                    "embedding_model defaulting to %s (OPENAI_API_KEY present, "
                    "no NEXUS_EMBEDDING_MODEL set)",
                    embedding_model,
                )
            if embedding_model:
                self._embedding_client = EmbeddingClient(
                    model=embedding_model,
                    dim=getattr(self.config, "embedding_dimensions", 1536) or 1536,
                )
                if self._embedding_provider is None:
                    self._embedding_provider = self._embedding_client
        except Exception:
            logger.warning("EmbeddingClient init failed", exc_info=True)
            self._embedding_client = None

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
            # Central per-directory semantic index scope gate (Issue #3698).
            # The daemon owns the mutable scope state; the pipeline asks for
            # a fresh snapshot on every call so in-flight registrations are
            # reflected without cross-thread gymnastics.
            scope_provider=self._current_index_scope,
            # Codex review R6 (high): forward the SANDBOX vec backend
            # so daemon refresh writes mirror into the hybrid vector
            # lane.
            sqlite_vec_backend=self._sqlite_vec_backend,
        )
        if self._async_session is not None:
            self._chunk_store = ChunkStore(
                async_session_factory=self._async_session,
                db_type=_db_type,
            )
        self._mutation_resolver = MutationResolver(
            file_reader=self._file_reader,
            async_session_factory=self._async_session,
        )

        # Start durable mutation consumers
        if self.config.refresh_enabled:
            self._refresh_task = asyncio.create_task(self._index_refresh_loop())
            self._legacy_refresh_task = asyncio.create_task(self._legacy_refresh_loop())

        # Start the periodic scope refresh loop so multi-worker
        # deployments converge on the same scope state when another
        # worker mutated it. Single-worker deployments still benefit
        # because external SQL changes (admin tools, migrations) are
        # picked up without a daemon restart.
        if self._async_session is not None and self.config.scope_refresh_seconds > 0:
            self._scope_refresh_task = asyncio.create_task(self._scope_refresh_loop())

        # Bootstrap skeleton index from DB rows (Issue #3725, 13B — no file reads).
        # Warm after bootstrap so the first real /locate query is fast (16A).
        if self._async_session is not None:
            self._skeleton_bootstrap_task = asyncio.create_task(self._bootstrap_skeleton())
            # Chain warmup probe as a non-blocking follow-on
            asyncio.create_task(self._warm_skeleton_index())

        self._initialized = True
        self.stats.startup_time_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            "SearchDaemon ready in %.1fms - DB pool: %d connections",
            self.stats.startup_time_ms,
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
        for task in self._consumer_tasks.values():
            task.cancel()

        if self._skeleton_bootstrap_task and not self._skeleton_bootstrap_task.done():
            self._skeleton_bootstrap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._skeleton_bootstrap_task
        self._skeleton_bootstrap_task = None
        if self._legacy_refresh_task:
            self._legacy_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._legacy_refresh_task
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        if self._scope_refresh_task:
            self._scope_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scope_refresh_task
            self._scope_refresh_task = None
        self._consumer_tasks.clear()

        # Shutdown search backends (Issue #3699)
        for backend_attr in ("_fts_backend", "_vector_backend"):
            backend = getattr(self, backend_attr, None)
            if backend is not None:
                try:
                    await backend.shutdown()
                except Exception as e:
                    logger.debug("%s shutdown error: %s", backend_attr, e)
                setattr(self, backend_attr, None)
        self._embedding_client = None

        # Close database connections (only if we created them).
        # Issue #3775: aclose() disposes async engines on this loop (their
        # origin); close() then disposes the sync engine. Daemon owns no
        # close-callback chain so the two can run back-to-back. Wrap aclose
        # in try/finally so a dispose failure does not abort the rest of
        # teardown (sync close, daemon state clear, path-context cleanup) —
        # otherwise _shutting_down stays set and a retry early-exits, making
        # this a persistent leak.
        if self._owns_engine:
            if self._record_store is not None:
                aclose_fn = getattr(self._record_store, "aclose", None)
                try:
                    if aclose_fn is not None:
                        await aclose_fn()
                except Exception:
                    logger.warning(
                        "record_store.aclose failed during daemon shutdown; "
                        "continuing with sync close",
                        exc_info=True,
                    )
                finally:
                    try:
                        self._record_store.close()
                    except Exception:
                        logger.warning(
                            "record_store.close failed during daemon shutdown",
                            exc_info=True,
                        )
                    self._record_store = None
            elif self._async_engine:
                try:
                    await self._async_engine.dispose()
                except Exception:
                    logger.warning(
                        "async_engine.dispose failed during daemon shutdown",
                        exc_info=True,
                    )
        self._async_engine = None
        self._async_session = None

        # Dispose loop-local path-context engines we created lazily. Only
        # engines whose origin loop is the current running loop can actually
        # be disposed from here — asyncpg pools for a different (or dead)
        # loop must be released on their own loop, which we cannot reach
        # from shutdown. Dropping the references is still correct: the dead
        # loop's socket fds are reclaimed when it's garbage-collected.
        # Suppress the expected cross-loop error on dispose rather than
        # letting it surface as a shutdown warning.
        running_loop = asyncio.get_running_loop()
        for loop_key, engine in list(self._path_context_engines_by_loop.items()):
            # loop_key is running_loop => this code runs on that loop, so
            # it is by definition not closed. Other loops cannot be
            # disposed safely from here; drop the reference and let GC
            # reclaim their sockets.
            if loop_key is running_loop:
                with contextlib.suppress(Exception):
                    await engine.dispose()
        self._path_context_engines_by_loop.clear()
        self._path_context_cache_by_loop.clear()

        self._initialized = False
        logger.info("SearchDaemon shutdown complete")

    # BackgroundService protocol aliases
    start = startup
    stop = shutdown

    # =========================================================================
    # Initialization Methods
    # =========================================================================

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
            assert self._async_engine is not None  # Set on line 429
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

    # ==========================================================================
    # Skeleton index — Issue #3725
    # ==========================================================================

    async def _bootstrap_skeleton(self) -> None:
        """Load document_skeleton rows from DB into _skeleton_docs (13B).

        No file reads: DB rows are the authoritative cache.  Rebuilds the
        in-memory index on every daemon restart.  Runs as a background task
        so startup() is not blocked.
        """
        if self._async_session is None:
            return

        start = time.perf_counter()
        count = 0
        try:
            from nexus.bricks.search.text_utils import tokenize_path

            async with self._async_session() as session:
                result = await session.execute(
                    sa_text(
                        "SELECT ds.path_id, ds.zone_id, ds.title, fp.virtual_path "
                        "FROM document_skeleton ds "
                        "JOIN file_paths fp ON fp.path_id = ds.path_id "
                        "WHERE fp.deleted_at IS NULL"
                    )
                )
                rows = result.fetchall()

            for row in rows:
                path_id, zone_id, title, virtual_path = row
                if not virtual_path:
                    continue
                self._skeleton_docs[virtual_path] = {
                    "path_id": path_id,
                    "zone_id": zone_id,
                    "title": title,
                    "path_tokens": tokenize_path(virtual_path),
                }
                count += 1

            self._skeleton_bootstrapped = True
            elapsed = (time.perf_counter() - start) * 1000
            logger.info("[SKELETON] bootstrap complete: %d docs in %.1fms", count, elapsed)

        except Exception as e:
            logger.warning("[SKELETON] bootstrap failed: %s", e)

    async def _warm_skeleton_index(self) -> None:
        """Wait for skeleton bootstrap and log a warmup probe result (16A).

        Fires a dummy locate() call to ensure the in-memory index is ready
        and log timing for the first real query.
        """
        if self._skeleton_bootstrap_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._skeleton_bootstrap_task), timeout=10.0)
        if self._skeleton_bootstrapped:
            # Probe query — forces any lazy work and logs timing
            await self.locate("warmup", zone_id=ROOT_ZONE_ID, limit=1)
            logger.debug("[SKELETON] index warmed: %d docs", len(self._skeleton_docs))

    def upsert_skeleton_doc(
        self,
        *,
        path_id: str,
        virtual_path: str,
        title: str | None,
        zone_id: str,
    ) -> None:
        """Upsert a skeleton document into the in-memory index (sync, called by SkeletonIndexer)."""
        from nexus.bricks.search.text_utils import tokenize_path

        self._skeleton_docs[virtual_path] = {
            "path_id": path_id,
            "zone_id": zone_id,
            "title": title,
            "path_tokens": tokenize_path(virtual_path),
        }

    def delete_skeleton_doc(self, *, virtual_path: str, zone_id: str) -> None:  # noqa: ARG002
        """Remove a skeleton document from the in-memory index (sync).

        zone_id accepted for API symmetry with upsert_skeleton_doc; not used
        here because _skeleton_docs is keyed by virtual_path.
        """
        self._skeleton_docs.pop(virtual_path, None)

    async def locate(
        self,
        q: str,
        *,
        zone_id: str,
        limit: int = 20,
        path_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25-lite path+title search over the skeleton index (Issue #3725).

        Tokenizes q, scores each document by token overlap with title (weight 2)
        and path_tokens (weight 1), returns top-limit results sorted by score.

        Zone isolation: only documents whose zone_id == zone_id are returned.

        Args:
            q: Natural-language or keyword query.
            zone_id: Caller's zone — results are filtered to this zone.
            limit: Maximum number of candidates to return.
            path_prefix: Optional path prefix filter (e.g. "/workspace/src/").

        Returns:
            List of dicts: {path, score, title}.
        """
        from nexus.bricks.search.text_utils import tokenize_path

        if not q.strip():
            return []

        query_tokens = set(tokenize_path(q).split())
        if not query_tokens:
            return []

        scored: list[tuple[float, str, str | None]] = []  # (score, path, title)

        for virtual_path, doc in self._skeleton_docs.items():
            if doc["zone_id"] != zone_id:
                continue
            if path_prefix and not virtual_path.startswith(path_prefix):
                continue

            path_tokens = set((doc.get("path_tokens") or "").split())
            title_tokens = set(tokenize_path(doc.get("title") or "").split())

            # Title match weighted higher than path match (review decision 6A)
            title_overlap = len(query_tokens & title_tokens)
            path_overlap = len(query_tokens & path_tokens)
            score = title_overlap * 2.0 + path_overlap * 1.0

            if score > 0:
                scored.append((score, virtual_path, doc.get("title")))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            {"path": path, "score": round(score, 4), "title": title}
            for score, path, title in scored[:limit]
        ]

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

    async def _resolve_path_context_cache(self) -> Any | None:
        """Return a PathContextCache bound to the current running loop.

        Issue #3773 note: the startup-time cache is bound to the lifespan
        loop. Under BaseHTTPMiddleware the request runs on a different loop,
        and asyncpg raises ``got result for unknown protocol state`` when
        used cross-loop. Lazily create a request-loop-native cache from
        ``DATABASE_URL`` and memoize it per loop.
        """
        import os

        from nexus.bricks.search.path_context import PathContextCache, PathContextStore

        loop = asyncio.get_running_loop()
        # Round-7 review: prune closed-loop entries to bound memory on
        # long-running servers where request loops can come and go (worker
        # recycling, anyio loop churn). Entries for dead loops can't be
        # disposed from here anyway — shutdown can only reach the current
        # loop's engine — so dropping their refs is the correct cleanup.
        # Round-8 review: tolerate non-loop keys (mocks, test fixtures) — a
        # missing ``is_closed`` attr is treated as closed so the lookup
        # never raises AttributeError.
        stale = [
            lk
            for lk in self._path_context_cache_by_loop
            if not hasattr(lk, "is_closed") or lk.is_closed()
        ]
        for lk in stale:
            self._path_context_cache_by_loop.pop(lk, None)
            self._path_context_engines_by_loop.pop(lk, None)
        existing = self._path_context_cache_by_loop.get(loop)
        if existing is not None:
            return existing

        # Prefer the explicit DaemonConfig URL (set by `create_app(database_url=...)`
        # or the startup lifespan), fall back to env vars. Both must be consulted
        # — env-only lookup misses the `create_app(database_url=...)` code path
        # (Issue #3773 review feedback).
        db_url = (
            self.config.database_url
            or os.environ.get("DATABASE_URL")
            or os.environ.get("NEXUS_DATABASE_URL")
        )
        if not db_url:
            if self._path_context_cache is not None:
                self._path_context_cache_by_loop[loop] = self._path_context_cache
            return self._path_context_cache

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            db_type = "postgresql"
        elif db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
            db_type = "postgresql"
        elif db_url.startswith("sqlite:") and "+aiosqlite" not in db_url:
            db_url = db_url.replace("sqlite:", "sqlite+aiosqlite:", 1)
            db_type = "sqlite"
        else:
            db_type = "sqlite"

        engine = create_async_engine(db_url, future=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = PathContextStore(async_session_factory=factory, db_type=db_type)
        cache = PathContextCache(store=store, max_zones=self.config.path_context_max_zones)
        self._path_context_cache_by_loop[loop] = cache
        self._path_context_engines_by_loop[loop] = engine
        return cache

    async def _attach_path_contexts(
        self,
        results: list[SearchResult],
        *,
        zone_id: str | None = None,
    ) -> None:
        """Attach admin-configured path context descriptions to search results.

        Issue #3773: When a ``PathContextCache`` is wired in, look up the
        longest-prefix context for each result and populate
        ``SearchResult.context`` in-place. No-op when the cache is absent or
        the result list is empty. Refreshes per-zone cache at most once per
        batch, then performs pure in-memory lookups (Issue #3773 review).

        ``zone_id`` is the effective zone scope of the caller's search.
        Many backend paths (PgFtsBackend, legacy BM25) construct ``SearchResult``
        without populating ``result.zone_id``, so we cannot rely on the
        per-result field. Use the caller-supplied ``zone_id`` as the
        authoritative fallback, otherwise non-root zone searches silently
        attach root-zone contexts (Round-3 review regression).

        Fails soft: if the cache lookup raises (e.g. asyncpg loop mismatch),
        logs a warning and leaves ``context`` unset rather than breaking
        the whole search.
        """
        if not results:
            return
        from nexus.contracts.constants import ROOT_ZONE_ID

        try:
            cache = await self._resolve_path_context_cache()
        except Exception as exc:
            self.stats.path_context_resolve_failures += 1
            logger.warning(
                "path context cache resolution failed (total=%d): %s",
                self.stats.path_context_resolve_failures,
                exc,
            )
            return
        if cache is None:
            return

        # Prefer the caller's effective zone over per-result zone_id because
        # most backends don't populate it; fall back to r.zone_id then to
        # ROOT_ZONE_ID if both are absent.
        effective_zone = zone_id or ROOT_ZONE_ID

        def _zone_for(r: SearchResult) -> str:
            return r.zone_id or effective_zone

        zones = {_zone_for(r) for r in results}
        # Refresh + synchronously snapshot each zone's records list (between
        # awaits a concurrent request could evict this zone from the LRU).
        # snapshot_zone is sync so the grab happens before the next refresh's
        # await point. Isolate per-zone failures: one zone raising shouldn't
        # drop context for every other zone in the same batch (Round-4 review).
        snapshots: dict[str, list[Any]] = {}
        for zone in zones:
            try:
                await cache.refresh_if_stale(zone)
            except Exception as exc:
                # Round-5 review: a transient DB error during refresh must not
                # discard a previously-cached snapshot. Stale context is
                # strictly better than no context for an LLM consumer, and it
                # matches the fail-soft contract advertised in the stats
                # counter's name.
                self.stats.path_context_attach_failures += 1
                logger.warning(
                    "path context refresh failed for zone=%r (total=%d): %s",
                    zone,
                    self.stats.path_context_attach_failures,
                    exc,
                )
            # Round-7 review: snapshot_zone is a dict access but the fail-soft
            # contract says ONE zone raising must not propagate out of this
            # method. Wrap it too so a rare concurrent-mutation RuntimeError
            # is caught and counted, not raised.
            try:
                snap = cache.snapshot_zone(zone)
            except Exception as exc:
                self.stats.path_context_attach_failures += 1
                logger.warning(
                    "path context snapshot failed for zone=%r (total=%d): %s",
                    zone,
                    self.stats.path_context_attach_failures,
                    exc,
                )
                snap = None
            if snap is not None:
                snapshots[zone] = snap

        from nexus.bricks.search.path_context import lookup_in_records

        for r in results:
            zone = _zone_for(r)
            records = snapshots.get(zone)
            if records is None:
                continue
            try:
                r.context = lookup_in_records(records, r.path)
            except Exception as exc:
                self.stats.path_context_attach_failures += 1
                logger.warning(
                    "path context lookup failed for path=%r (total=%d): %s",
                    r.path,
                    self.stats.path_context_attach_failures,
                    exc,
                )

    async def search(
        self,
        query: str,
        search_type: Literal["keyword", "semantic", "hybrid"] = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        zone_id: str | None = None,
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

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID
        start = time.perf_counter()
        self.last_search_timing = {}
        hybrid_keyword_results: list[SearchResult] = []
        hybrid_keyword_ms = 0.0

        try:
            if search_type == "keyword":
                # Keyword mode should use the daemon's keyword stack first.
                # The new fts backend is the canonical BM25 path. We still
                # try `_keyword_search` (Zoekt/BM25S/inline FTS) first when
                # zone-safe to preserve the existing fast-path semantics.
                keyword_start = time.perf_counter()
                keyword_results = await self._keyword_search(
                    query,
                    limit,
                    path_filter,
                    zone_id=effective_zone_id,
                )
                keyword_ms = (time.perf_counter() - keyword_start) * 1000
                self.last_search_timing = {"backend_ms": keyword_ms, "rerank_ms": 0.0}
                if keyword_results:
                    latency_ms = (time.perf_counter() - start) * 1000
                    self._track_latency(latency_ms)
                    await self._attach_path_contexts(keyword_results, zone_id=effective_zone_id)
                    return keyword_results
            elif search_type == "hybrid":
                # Make lexical candidates explicit in hybrid mode so exact
                # matches are not lost before backend semantic ranking.
                keyword_start = time.perf_counter()
                hybrid_keyword_results = await self._keyword_search(
                    query,
                    limit * 3,
                    path_filter,
                    zone_id=effective_zone_id,
                )
                hybrid_keyword_ms = (time.perf_counter() - keyword_start) * 1000

            # Delegate to the new search backends (Issue #3699). The daemon
            # now owns hybrid composition: chunk-BM25 + page-BM25 + dense,
            # fused via the fusion module. SQLite drops the page-BM25 leg.
            if self._fts_backend is not None and self._vector_backend is not None:
                backend_start = time.perf_counter()
                backend_results = await self._search_via_backends(
                    query,
                    search_type=search_type,
                    limit=limit,
                    path_filter=path_filter,
                    zone_id=effective_zone_id,
                )
                backend_ms = (time.perf_counter() - backend_start) * 1000
                self.last_search_timing = {
                    "backend_ms": backend_ms,
                    "rerank_ms": 0.0,
                }
                if hybrid_keyword_ms:
                    self.last_search_timing["keyword_ms"] = hybrid_keyword_ms

                if backend_results:
                    results = backend_results
                    if search_type == "hybrid" and hybrid_keyword_results:
                        results = self._fuse_ranked_results(
                            hybrid_keyword_results,
                            results,
                            limit,
                        )

                    latency_ms = (time.perf_counter() - start) * 1000
                    self._track_latency(latency_ms)
                    await self._attach_path_contexts(results, zone_id=effective_zone_id)
                    return results
                # Backend returned empty — fall through to the legacy stack
                # so Zoekt / BM25S / inline FTS can still serve the query.

            # Legacy fallback (no search backends available, or empty result)
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
            if hybrid_keyword_ms and "backend_ms" in self.last_search_timing:
                self.last_search_timing["keyword_ms"] = hybrid_keyword_ms

            await self._attach_path_contexts(results, zone_id=effective_zone_id)
            return results

        except TimeoutError:
            logger.warning(f"Search timeout after {self.config.query_timeout_seconds}s")
            return []

    async def _search_via_backends(
        self,
        query: str,
        *,
        search_type: str,
        limit: int,
        path_filter: str | None,
        zone_id: str,
    ) -> list[SearchResult]:
        """Run keyword / semantic / hybrid via the new search backends.

        Hybrid mode performs 3-way RRF on Postgres (chunk-BM25 + page-BM25 +
        dense) and 2-way RRF on SQLite (chunk-BM25 + dense — there is no
        page-level BM25 leg yet on the FTS5 vtable).
        """
        from nexus.bricks.search.fusion import rrf_fusion
        from nexus.bricks.search.pg_fts_backend import PgFtsBackend

        path = path_filter or "/"

        if search_type == "keyword":
            results = await self._fts_backend.keyword_search(query, path, limit, zone_id)
            return [self._coerce_to_search_result(r, search_type=search_type) for r in results]

        if search_type == "semantic":
            qvec = await self._embed_query(query)
            if qvec is None:
                return []
            results = await self._vector_backend.semantic_search(qvec, path, limit, zone_id)
            return [self._coerce_to_search_result(r, search_type=search_type) for r in results]

        # Hybrid: 3-way RRF on PG, 2-way on SQLite.
        qvec = await self._embed_query(query)
        if qvec is None:
            # Without an embedding we still want a useful result — fall back
            # to keyword-only and let the caller decide if that's enough.
            results = await self._fts_backend.keyword_search(query, path, limit, zone_id)
            return [self._coerce_to_search_result(r, search_type=search_type) for r in results]

        is_pg = isinstance(self._fts_backend, PgFtsBackend)
        if is_pg:
            chunk_kw, page_kw, dense = await asyncio.gather(
                self._fts_backend.keyword_search(query, path, limit * 2, zone_id),
                self._fts_backend.keyword_search_pages(query, path, limit * 2, zone_id),
                self._vector_backend.semantic_search(qvec, path, limit * 2, zone_id),
                return_exceptions=False,
            )
        else:
            chunk_kw, dense = await asyncio.gather(
                self._fts_backend.keyword_search(query, path, limit * 2, zone_id),
                self._vector_backend.semantic_search(qvec, path, limit * 2, zone_id),
                return_exceptions=False,
            )
            page_kw = []

        # Fuse keyword legs first (chunk + page), then RRF that with dense.
        kw_fused = rrf_fusion(chunk_kw, page_kw, k=60, limit=limit * 2, id_key=None)
        fused = rrf_fusion(kw_fused, dense, k=60, limit=limit, id_key=None)
        return [self._coerce_to_search_result(item, search_type="hybrid") for item in fused]

    async def _embed_query(self, query: str) -> list[float] | None:
        """Embed a query string for the new vector backends.

        Prefers the EmbeddingClient instantiated at startup; falls back to
        the legacy ``_embedding_provider`` path so deployments that don't
        configure the new client still function.
        """
        if self._embedding_client is not None:
            try:
                vec: list[float] = await self._embedding_client.embed_query(query)
                return vec
            except Exception as exc:
                logger.debug("EmbeddingClient.embed_query failed: %s", exc)
        return await self._get_query_embedding(query)

    @staticmethod
    def _coerce_to_search_result(
        raw: Any,
        *,
        search_type: str,
    ) -> SearchResult:
        """Normalise BaseSearchResult / dict outputs into SearchResult."""
        if isinstance(raw, SearchResult):
            return raw
        if isinstance(raw, BaseSearchResult):
            return SearchResult(
                path=raw.path,
                chunk_index=raw.chunk_index,
                chunk_text=raw.chunk_text,
                score=raw.score,
                start_offset=raw.start_offset,
                end_offset=raw.end_offset,
                line_start=raw.line_start,
                line_end=raw.line_end,
                keyword_score=raw.keyword_score,
                vector_score=raw.vector_score,
                reranker_score=raw.reranker_score,
                zone_id=raw.zone_id,
                search_type=search_type,
            )
        if isinstance(raw, dict):
            return SearchResult(
                path=str(raw.get("path", "")),
                chunk_index=int(raw.get("chunk_index", 0)),
                chunk_text=str(raw.get("chunk_text", "")),
                score=float(raw.get("score", 0.0)),
                start_offset=raw.get("start_offset"),
                end_offset=raw.get("end_offset"),
                line_start=raw.get("line_start"),
                line_end=raw.get("line_end"),
                keyword_score=raw.get("keyword_score"),
                vector_score=raw.get("vector_score"),
                reranker_score=raw.get("reranker_score"),
                zone_id=raw.get("zone_id"),
                search_type=search_type,
            )
        # Unknown shape — best-effort fallback.
        return SearchResult(
            path=getattr(raw, "path", ""),
            chunk_text=getattr(raw, "chunk_text", ""),
            score=float(getattr(raw, "score", 0.0)),
            search_type=search_type,
        )

    @staticmethod
    def _fuse_ranked_results(
        keyword_results: list[SearchResult],
        backend_results: list[SearchResult],
        limit: int,
    ) -> list[SearchResult]:
        """Fuse daemon keyword and backend-ranked results with RRF."""
        rrf_k = 60
        scores: dict[tuple[str, str, int], float] = {}
        best: dict[tuple[str, str, int], SearchResult] = {}

        for source_results in (keyword_results, backend_results):
            for rank, result in enumerate(source_results):
                key = (result.zone_id or "", result.path, result.chunk_index)
                scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
                existing = best.get(key)
                if existing is None or result.score > existing.score:
                    best[key] = result

        fused: list[SearchResult] = []
        for key in sorted(scores, key=lambda item: scores[item], reverse=True)[:limit]:
            result = best[key]
            fused.append(
                SearchResult(
                    path=result.path,
                    chunk_text=result.chunk_text,
                    score=scores[key],
                    chunk_index=result.chunk_index,
                    start_offset=result.start_offset,
                    end_offset=result.end_offset,
                    line_start=result.line_start,
                    line_end=result.line_end,
                    keyword_score=result.keyword_score,
                    vector_score=result.vector_score,
                    splade_score=result.splade_score,
                    reranker_score=result.reranker_score,
                    matched_field=result.matched_field,
                    attribute_boost=result.attribute_boost,
                    original_score=result.original_score,
                    zone_id=result.zone_id,
                    context=result.context,
                    semantic_degraded=result.semantic_degraded,
                    search_type="hybrid",
                )
            )
        return fused

    async def batch_search(
        self,
        queries: list[dict[str, Any]],
        *,
        zone_id: str | None = None,
    ) -> list[list[Any]]:
        """Batch search: embed N queries in ONE API call.

        Powers ``POST /api/v2/search/query/batch`` for benchmarks and bulk
        evaluations. ~30s for 470 queries instead of ~16 min sequential.
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID
        # Issue #3699: dedicated backend.batch_search is gone with txtai; we
        # fan out per-query through the new backend stack instead. This is
        # functionally equivalent for callers, just without the single-call
        # embedding amortisation. T9 keeps the API surface intact.
        results: list[list[Any]] = []
        for q in queries:
            if not isinstance(q, dict):
                results.append([])
                continue
            try:
                hits = await self.search(
                    str(q.get("query", "")),
                    search_type=q.get("search_type", "hybrid"),
                    limit=int(q.get("limit", 10)),
                    path_filter=q.get("path_filter"),
                    zone_id=effective_zone_id,
                )
            except Exception as exc:
                logger.warning("batch_search inner search failed: %s", exc)
                hits = []
            results.append(hits)
        # Issue #3773: attach admin-configured path contexts. The whole batch
        # is single-zone by design (``zone_id=effective_zone_id`` above), so
        # refresh once against that zone and do pure in-memory lookups on
        # every inner result against the snapshot — backends return
        # ``BaseSearchResult`` without ``zone_id`` set, so we must use the
        # caller's scope instead of ``r.zone_id`` (Round-3 review).
        try:
            cache = await self._resolve_path_context_cache()
        except Exception as exc:
            self.stats.path_context_resolve_failures += 1
            logger.warning(
                "path context cache resolution failed (total=%d): %s",
                self.stats.path_context_resolve_failures,
                exc,
            )
            cache = None
        if cache is not None:
            # Round-6 review: refresh failure must not drop an otherwise-usable
            # stale snapshot — matches the fail-soft contract in
            # ``_attach_path_contexts``. Snapshot AFTER the refresh's try so a
            # transient DB error still yields the last successfully-loaded
            # records instead of silently erasing context for the whole batch.
            try:
                await cache.refresh_if_stale(effective_zone_id)
            except Exception as exc:
                self.stats.path_context_attach_failures += 1
                logger.warning(
                    "path context refresh failed for zone=%r (total=%d): %s",
                    effective_zone_id,
                    self.stats.path_context_attach_failures,
                    exc,
                )
            # Round-7 review: wrap snapshot_zone too so the fail-soft contract
            # holds even if a concurrent LRU mutation races the dict access.
            try:
                records = cache.snapshot_zone(effective_zone_id)
            except Exception as exc:
                self.stats.path_context_attach_failures += 1
                logger.warning(
                    "path context snapshot failed for zone=%r (total=%d): %s",
                    effective_zone_id,
                    self.stats.path_context_attach_failures,
                    exc,
                )
                records = None
            if records is not None:
                from nexus.bricks.search.path_context import lookup_in_records

                for inner in results:
                    for r in inner:
                        try:
                            r.context = lookup_in_records(records, r.path)
                        except Exception as exc:
                            self.stats.path_context_attach_failures += 1
                            logger.warning(
                                "path context lookup failed for path=%r (total=%d): %s",
                                r.path,
                                self.stats.path_context_attach_failures,
                                exc,
                            )
        return results

    async def index_documents(
        self,
        documents: list[dict[str, Any]],
        *,
        zone_id: str | None = None,
    ) -> int:
        """Explicitly upsert documents into the active search backend.

        This powers ``POST /api/v2/search/index`` for synthetic or externally
        generated documents that do not rely on the file-refresh pipeline.

        Issue #3699: writes are owned by ``ChunkStore.replace_document_chunks``
        via the indexing pipeline. Each document is treated as ``(path,
        text)`` — we resolve ``path_id`` from ``file_paths`` and dispatch
        to ``IndexingPipeline.index_document`` which chunks, embeds, and
        bulk-inserts into ``document_chunks``. Failures bubble up so the
        HTTP boundary returns 500 (Decision #18) instead of silently
        returning count=0.
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        if not documents:
            return 0

        if self._indexing_pipeline is None:
            raise RuntimeError(
                "index_documents: indexing pipeline is not initialised. "
                "Daemon must complete startup() before serving writes."
            )

        target_zone = zone_id or ROOT_ZONE_ID

        def _scrub(value: str) -> str:
            return value.replace("\x00", "") if "\x00" in value else value

        indexed = 0
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            text_field = doc.get("text") or doc.get("content")
            virtual_path = doc.get("path") or doc.get("id")
            if not isinstance(text_field, str) or not text_field.strip():
                continue
            if not isinstance(virtual_path, str) or not virtual_path:
                continue

            text_clean = _scrub(text_field)
            vp_clean = strip_zone_prefix(virtual_path)

            # Resolve path_id from file_paths so the indexing pipeline can
            # write document_chunks. Without a path_id we cannot persist —
            # surface that as an exception so the caller (router) returns
            # 500 instead of silently dropping the doc.
            path_id: str | None = None
            if self._async_session is not None:
                async with self._async_session() as session:
                    row = (
                        await session.execute(
                            sa_text(
                                "SELECT path_id FROM file_paths "
                                "WHERE virtual_path = :vp "
                                "  AND zone_id = :zid "
                                "  AND deleted_at IS NULL "
                                "LIMIT 1"
                            ),
                            {"vp": vp_clean, "zid": target_zone},
                        )
                    ).first()
                    if row is not None:
                        path_id = str(row[0])

            if path_id is None:
                # No file_paths row — this happens for synthetic docs
                # (skill READMEs, connector schemas) that aren't backed
                # by NexusFS. Skip rather than fail so best-effort callers
                # in mount/connector wiring don't error out.
                logger.debug(
                    "index_documents: no file_paths row for %s in zone %s; skipping",
                    vp_clean,
                    target_zone,
                )
                continue

            # Drive the canonical write path. Pass the zone-scoped form
            # so the pipeline's scope filter sees the same shape mutation
            # consumers use.
            scoped_path = (
                f"/zone/{target_zone}{vp_clean}" if target_zone != ROOT_ZONE_ID else vp_clean
            )
            result = await self._indexing_pipeline.index_document(scoped_path, text_clean, path_id)
            if result.error:
                # Surface pipeline errors so the router returns 500.
                raise RuntimeError(
                    f"index_documents: pipeline failed for {vp_clean!r}: {result.error}"
                )
            indexed += 1

        return indexed

    async def delete_documents(
        self,
        ids: list[str],
        *,
        zone_id: str | None = None,
    ) -> int:
        """Delete indexed documents by virtual path.

        Issue #3699: deletes are owned by ``ChunkStore.delete_document_chunks``.
        Each ``id`` is resolved to a ``path_id`` (stripping any ``/zone/{id}/``
        prefix) and the chunk store drops every row keyed by that path. Returns
        the count of paths that had chunks removed (best-effort — a path with
        no rows is not an error).
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        if not ids:
            return 0
        if self._chunk_store is None or self._async_session is None:
            logger.debug("delete_documents: no chunk_store or session, skipping (%d ids)", len(ids))
            return 0

        target_zone = zone_id or ROOT_ZONE_ID

        deleted = 0
        for raw_id in ids:
            if not isinstance(raw_id, str) or not raw_id:
                continue
            # Allow callers to pass either a virtual path or the txtai-era
            # "{zone_id}:{path}" id form. Strip the zone prefix in either
            # case so file_paths lookup stays zone-scoped via :zid.
            if ":" in raw_id and not raw_id.startswith("/"):
                _, _, vp = raw_id.partition(":")
            else:
                vp = raw_id
            vp_clean = strip_zone_prefix(vp)

            async with self._async_session() as session:
                row = (
                    await session.execute(
                        sa_text(
                            "SELECT path_id FROM file_paths "
                            "WHERE virtual_path = :vp "
                            "  AND zone_id = :zid "
                            "LIMIT 1"
                        ),
                        {"vp": vp_clean, "zid": target_zone},
                    )
                ).first()
            if row is None:
                continue
            await self._chunk_store.delete_document_chunks(str(row[0]))
            deleted += 1

        return deleted

    async def _keyword_search(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
        *,
        zone_id: str | None = None,
    ) -> list[SearchResult]:
        """Fast keyword search using BM25S or Zoekt."""
        from nexus.contracts.constants import ROOT_ZONE_ID

        results: list[SearchResult] = []

        # Zoekt and BM25S have no per-zone metadata, so they are only safe
        # for the root zone (single-tenant) or when zone_id is unset.
        # Non-root zone searches skip both to avoid cross-zone leakage.
        is_zone_safe = zone_id is None or zone_id == ROOT_ZONE_ID

        # Try Zoekt first (fastest, trigram-based)
        if self.stats.zoekt_available and is_zone_safe:
            zoekt_results = await self._search_zoekt(query, limit, path_filter)
            if zoekt_results:
                return zoekt_results

        # Final fallback: database FTS (zone-aware)
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
        """Vector similarity search via the active ``_vector_backend`` (Issue #3699).

        The previous inline halfvec SQL block has been moved into
        ``PgVectorBackend.semantic_search`` (and its SQLite counterpart
        in ``SqliteVecBackend``). This wrapper keeps the legacy method
        signature so existing callers (legacy fallback path in
        :meth:`search`) work unchanged.
        """
        if self._vector_backend is None:
            return []

        try:
            embedding = await self._embed_query(query)
            if not embedding:
                logger.debug("Semantic search: no query embedding available")
                return []

            from nexus.contracts.constants import ROOT_ZONE_ID

            effective_zone_id = zone_id or ROOT_ZONE_ID
            path = path_filter or "/"
            results = await self._vector_backend.semantic_search(
                embedding, path, limit, effective_zone_id
            )
            return [self._coerce_to_search_result(r, search_type="semantic") for r in results]
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

        Note: this is the legacy embedding path used as a fallback when no
        ``EmbeddingClient`` is configured. The new search backends call
        :meth:`_embed_query` which prefers ``EmbeddingClient`` first.
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
        """Wake durable mutation consumers after a filesystem change.

        The durable source of truth is the operation log. Hooks only wake the
        consumer loop so successful writes are reflected quickly. A legacy
        in-process fallback queue is kept for stacks where the operation log
        is absent or not populated for local file mutations.
        """
        if not self.config.refresh_enabled:
            return

        if self._mutation_resolver is not None:
            self._mutation_resolver.invalidate_path(path)
        if change_type == "delete":
            self._pending_delete_paths.add(path)
            self._pending_refresh_paths.discard(path)
        else:
            self._pending_refresh_paths.add(path)
            self._pending_delete_paths.discard(path)
        self._mutation_wakeup.set()

    async def _index_refresh_loop(self) -> None:
        """Background task to drive durable per-indexer mutation consumers."""
        consumer_specs = {
            "fts": self._consume_fts_mutations,
            "embedding": self._consume_embedding_mutations,
        }
        self._consumer_names = tuple(consumer_specs.keys())
        # #4016: reconcile pre-existing unindexed files BEFORE consumers
        # snap their checkpoints to MAX(sequence_number). Skipped on warm
        # restarts (any consumer already has a persisted checkpoint).
        await self._reconcile_unindexed_paths_at_startup()
        for consumer_name in self._consumer_names:
            if consumer_name not in self._consumer_last_sequence:
                self._consumer_last_sequence[
                    consumer_name
                ] = await self._initialize_consumer_checkpoint(consumer_name)
        self._consumer_tasks = {
            name: asyncio.create_task(
                self._run_mutation_consumer(name, handler), name=f"search-{name}"
            )
            for name, handler in consumer_specs.items()
        }
        try:
            await asyncio.gather(*self._consumer_tasks.values())
        finally:
            self._consumer_tasks.clear()

    async def _legacy_refresh_loop(self) -> None:
        """Fallback hook-driven refresh path for stacks without usable op-log events."""
        while not self._shutting_down:
            try:
                await self._mutation_wakeup.wait()
                await asyncio.sleep(self.config.refresh_debounce_seconds)
                self._mutation_wakeup.clear()

                async with self._refresh_lock:
                    # _coalesce_subtrees may return directory sentinels,
                    # but _refresh_indexes only handles concrete file paths.
                    # Keep file-granular paths until _refresh_indexes is
                    # directory-aware (Issue #3708, #3148).
                    refresh_paths = sorted(self._pending_refresh_paths)
                    delete_paths = sorted(self._pending_delete_paths)
                    if not refresh_paths and not delete_paths:
                        continue
                    self._pending_refresh_paths.clear()
                    self._pending_delete_paths.clear()

                if delete_paths:
                    await self._delete_indexes_for_paths(delete_paths)
                if refresh_paths:
                    await self._refresh_indexes(refresh_paths)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Legacy search refresh loop failed: %s", exc)
                await asyncio.sleep(1)

    async def _delete_indexes_for_paths(self, paths: list[str]) -> None:
        """Best-effort delete propagation for fallback stacks without op-log delivery."""
        if not paths:
            return

        logger.debug("delete-propagation: start paths=%s", paths)

        events = [
            SearchMutationEvent(
                event_id=f"legacy-delete:{path}",
                operation_id=f"legacy-delete:{path}",
                op=SearchMutationOp.DELETE,
                path=path,
                zone_id=extract_zone_id(path),
                timestamp=datetime.now(UTC).replace(tzinfo=None),
                sequence_number=0,
            )
            for path in paths
        ]
        resolved = await self._resolve_mutations(events)
        logger.debug(
            "delete-propagation: resolved=%d (resolved_path_id=%d)",
            len(resolved),
            sum(1 for m in resolved if self._has_resolved_path_id(m)),
        )
        if not resolved:
            return

        if self._chunk_store is not None:
            chunks_deleted = 0
            for mutation in resolved:
                if self._has_resolved_path_id(mutation):
                    await self._chunk_store.delete_document_chunks(mutation.path_id)
                    chunks_deleted += 1
            logger.debug(
                "delete-propagation: chunk_store dropped %d/%d documents",
                chunks_deleted,
                len(resolved),
            )

        # Issue #3699: txtai backend delete propagation is gone — chunk_store
        # above already cascades into the FTS5 / pg_textsearch / pgvector
        # indexes via DB triggers / index maintenance. The new search
        # backends don't own write paths, so no second delete leg is needed.

        # Codex review R8 #4 (high): the legacy refresh path is the
        # fallback delete carrier when the durable op-log consumer
        # isn't wired (older deployments, recovery boots). Without
        # this prune the SANDBOX vec lane retained rows for deleted/
        # renamed paths while ChunkStore/BM25/txtai lanes were
        # cleaned up, leading to zombie hits in the semantic lane.
        if self._sqlite_vec_backend is not None:
            # Codex review R9 #3 (high): canonical (unscoped virtual_path)
            # + legacy (scoped event.path) keys per zone so legacy rows
            # from pre-R9 builds also get pruned during recovery boots.
            vec_deletes_by_zone: dict[str, list[str]] = {}
            for mutation in resolved:
                bucket = vec_deletes_by_zone.setdefault(mutation.zone_id, [])
                if mutation.virtual_path not in bucket:
                    bucket.append(mutation.virtual_path)
                if (
                    mutation.event.path != mutation.virtual_path
                    and mutation.event.path not in bucket
                ):
                    bucket.append(mutation.event.path)
            for zone_id, vec_paths in vec_deletes_by_zone.items():
                with contextlib.suppress(Exception):
                    await self._sqlite_vec_backend.delete(vec_paths, zone_id=zone_id)
                    logger.debug(
                        "delete-propagation: sqlite-vec dropped %d path(s) for zone=%s",
                        len(vec_paths),
                        zone_id,
                    )

        logger.info("delete-propagation: completed for %d path(s)", len(paths))

    @staticmethod
    def _coalesce_subtrees(
        paths: set[str],
        threshold: int = 20,
    ) -> list[str]:
        """Coalesce many file paths under the same directory into one entry.

        When a sync writes hundreds of files under the same subtree, indexing
        each individually is wasteful. This groups paths by parent directory
        and replaces groups larger than ``threshold`` with the parent dir.

        Smaller groups (< threshold) are returned as individual paths.

        Issue #3148, Decision #13B: debounce in search daemon.

        Args:
            paths: Set of file paths to coalesce.
            threshold: Minimum paths per directory to trigger coalescing.

        Returns:
            Coalesced list of paths (may include directories).
        """
        if len(paths) < threshold:
            return list(paths)

        import posixpath
        from collections import Counter

        # Count paths per parent directory
        parent_counts: Counter[str] = Counter()
        for p in paths:
            parent = posixpath.dirname(p)
            parent_counts[parent] += 1

        result: list[str] = []
        coalesced_parents: set[str] = set()

        for parent, count in parent_counts.items():
            if count >= threshold:
                result.append(parent)
                coalesced_parents.add(parent)

        # Add paths whose parents were NOT coalesced
        for p in paths:
            if posixpath.dirname(p) not in coalesced_parents:
                result.append(p)

        return result

    @staticmethod
    def _has_resolved_path_id(mutation: ResolvedMutation) -> bool:
        return bool(
            getattr(
                mutation,
                "path_id_resolved",
                mutation.path_id != mutation.virtual_path,
            )
        )

    @staticmethod
    def _path_lookup_candidates(zone_id: str, scoped_path: str, virtual_path: str) -> list[str]:
        candidates: list[str] = []

        def add(candidate: str) -> None:
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        add(virtual_path)
        add(scoped_path)
        if virtual_path.startswith("/"):
            add(f"/zone/{zone_id}{virtual_path}")
        return candidates

    @staticmethod
    def _build_path_lookup_values(candidates: list[str]) -> tuple[str, dict[str, str | int]]:
        params: dict[str, str | int] = {}
        values_parts: list[str] = []
        for idx, candidate in enumerate(candidates):
            params[f"candidate_{idx}"] = candidate
            params[f"rank_{idx}"] = idx
            values_parts.append(f"(:candidate_{idx}, CAST(:rank_{idx} AS INTEGER))")
        return ", ".join(values_parts), params

    @staticmethod
    def _build_naive_chunks(content: str, chunk_size: int = 1000) -> list[ChunkRecord]:
        """Build naive fixed-size chunk records for FTS fallback."""
        raw = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
        records: list[ChunkRecord] = []
        for idx, chunk_text in enumerate(raw):
            if not chunk_text.strip():
                continue
            preceding = content[: idx * chunk_size]
            line_start = preceding.count("\n") + 1
            line_end = line_start + chunk_text.count("\n")
            records.append(
                ChunkRecord(
                    chunk_text=chunk_text,
                    chunk_tokens=max(1, len(chunk_text) // 4),
                    start_offset=idx * chunk_size,
                    end_offset=idx * chunk_size + len(chunk_text),
                    line_start=line_start,
                    line_end=line_end,
                )
            )
        return records

    async def _index_to_document_chunks(self, path_id: str, path: str, content: str) -> None:
        """Insert content as document_chunks for FTS search."""
        if self._chunk_store is None:
            return

        try:
            records = self._build_naive_chunks(content)
            await self._chunk_store.replace_document_chunks(path_id, records)
        except Exception as e:
            logger.debug("Failed to index %s to document_chunks: %s", path, e)
            return

        # Codex review R7 (high): mirror naive-chunk writes into the
        # SANDBOX vec backend. The embedding consumer's _bulk_insert
        # path is wired up, but it only fires when an embedding
        # provider is configured — current txtai-era wiring sets
        # provider=None, so the FTS consumer is the SOLE carrier of
        # production writes. Without this mirror, the SANDBOX hybrid
        # vec lane stays empty in real use. Best-effort: failures
        # log but don't break the primary FTS write.
        if self._sqlite_vec_backend is None:
            return
        try:
            from nexus.bricks.search.mutation_events import extract_zone_id, strip_zone_prefix

            zone_id = extract_zone_id(path)
            # Codex review R9 #3 (high): vec rows MUST be keyed on the
            # unscoped virtual_path so they line up with BM25
            # (``mutation.virtual_path``), the IndexingPipeline writer
            # (unscoped), and the SearchService ``path_filter``
            # (unscoped). Mixing scoped /zone/<zone>/foo.md with
            # unscoped /foo.md leaves doppelgänger rows that ignore
            # path-filter prefix matches and break dedup with BM25.
            canonical_path = strip_zone_prefix(path) if path.startswith("/zone/") else path
            # Full-replace: drop all prior vec rows for (zone_id, path)
            # so a doc shrinking from N to fewer chunks doesn't leave
            # stale higher-index rows searchable. Mirrors ChunkStore's
            # replace_document_chunks contract. Pass BOTH the canonical
            # and the original (possibly scoped) form so legacy rows
            # written before R9 also get pruned.
            delete_keys = [canonical_path]
            if path != canonical_path:
                delete_keys.append(path)
            await self._sqlite_vec_backend.delete(delete_keys, zone_id=zone_id)
            if records:
                items = [
                    {
                        "path": canonical_path,
                        "text": rec.chunk_text,
                        "chunk_index": i,
                    }
                    for i, rec in enumerate(records)
                ]
                await self._sqlite_vec_backend.upsert(items, zone_id=zone_id)
        except Exception as exc:
            logger.warning(
                "[SearchDaemon] sqlite-vec FTS-path mirror failed for %s "
                "(hybrid vec lane will degrade for this doc): %s",
                path,
                exc,
            )

    def _checkpoint_key(self, consumer_name: str) -> str:
        return f"search_mutation_checkpoint:{consumer_name}"

    async def _load_checkpoint_file(self) -> dict[str, int]:
        def _read() -> dict[str, int]:
            if not self._checkpoint_file.exists():
                return {}
            try:
                payload = json.loads(self._checkpoint_file.read_text())
            except (OSError, json.JSONDecodeError):
                return {}
            if not isinstance(payload, dict):
                return {}
            checkpoints: dict[str, int] = {}
            for key, value in payload.items():
                with contextlib.suppress(TypeError, ValueError):
                    checkpoints[str(key)] = int(value)
            return checkpoints

        return await asyncio.to_thread(_read)

    async def _write_checkpoint_file(self, checkpoints: dict[str, int]) -> None:
        def _write() -> None:
            self._checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_file.write_text(json.dumps(checkpoints, sort_keys=True))

        await asyncio.to_thread(_write)

    async def _read_persisted_checkpoint(self, consumer_name: str) -> int | None:
        if self._settings_store is not None:
            try:
                setting = self._settings_store.get_setting(self._checkpoint_key(consumer_name))
                if setting is not None:
                    with contextlib.suppress(ValueError):
                        return int(setting.value)
            except Exception as exc:
                logger.warning(
                    "Search mutation checkpoints falling back to file storage: %s",
                    exc,
                )
                self._settings_store = None

        async with self._checkpoint_lock:
            checkpoints = await self._load_checkpoint_file()
        return checkpoints.get(consumer_name)

    async def _persist_checkpoint(self, consumer_name: str, sequence_number: int) -> None:
        if self._settings_store is not None:
            try:
                self._settings_store.set_setting(
                    self._checkpoint_key(consumer_name),
                    str(sequence_number),
                    description=f"Search mutation checkpoint for {consumer_name}",
                )
                return
            except Exception as exc:
                logger.warning(
                    "Search mutation checkpoints falling back to file storage: %s",
                    exc,
                )
                self._settings_store = None

        async with self._checkpoint_lock:
            checkpoints = await self._load_checkpoint_file()
            checkpoints[consumer_name] = sequence_number
            await self._write_checkpoint_file(checkpoints)

    async def _initialize_consumer_checkpoint(self, consumer_name: str) -> int:
        persisted = await self._read_persisted_checkpoint(consumer_name)
        if persisted is not None:
            return persisted

        max_sequence = 0
        if self._async_session is not None:
            async with self._async_session() as session:
                row = (
                    await session.execute(
                        sa_text("SELECT COALESCE(MAX(sequence_number), 0) FROM operation_log")
                    )
                ).first()
                max_sequence = int(row[0]) if row and row[0] is not None else 0
        await self._save_consumer_checkpoint(consumer_name, max_sequence)
        return max_sequence

    def _reconciliation_marker_key(self, consumer_name: str) -> str:
        """Distinct namespace from ``_checkpoint_key`` so a snapped checkpoint
        on its own does NOT imply reconciliation has run (Codex round-1)."""
        return f"search_mutation_reconciled_v1:{consumer_name}"

    async def _reconciliation_completed(self, consumer_name: str) -> bool:
        if self._settings_store is not None:
            try:
                setting = self._settings_store.get_setting(
                    self._reconciliation_marker_key(consumer_name)
                )
                if setting is not None:
                    return True
            except Exception as exc:
                logger.warning(
                    "Reconciliation marker read falling back to file storage: %s",
                    exc,
                )
        async with self._checkpoint_lock:
            checkpoints = await self._load_checkpoint_file()
        return self._reconciliation_marker_key(consumer_name) in checkpoints

    async def _mark_reconciliation_completed(self, consumer_name: str) -> None:
        if self._settings_store is not None:
            try:
                self._settings_store.set_setting(
                    self._reconciliation_marker_key(consumer_name),
                    "1",
                    description=f"Search reconciliation completed for {consumer_name}",
                )
                return
            except Exception as exc:
                logger.warning(
                    "Reconciliation marker write falling back to file storage: %s",
                    exc,
                )
        async with self._checkpoint_lock:
            checkpoints = await self._load_checkpoint_file()
            checkpoints[self._reconciliation_marker_key(consumer_name)] = 1
            await self._write_checkpoint_file(checkpoints)

    async def _reconcile_unindexed_paths_at_startup(self) -> None:
        """Index live files that pre-date the daemon's first reconciliation (#4016).

        On first start, ``_initialize_consumer_checkpoint`` skips past every
        historical write by capturing ``MAX(operation_log.sequence_number)``.
        Files written before the daemon ever ran have no ``document_chunks``
        row — without this method, they'd stay unindexed until something
        else touches them (a manual ``semantic_search_index`` call, a fresh
        write to the same path, or a mount-content scan).

        Idempotent and per-consumer (Codex round-1):
        - Each consumer carries a ``search_mutation_reconciled_v1:<name>``
          marker, separate from its mutation checkpoint. Reconciliation
          runs whenever any consumer is missing the marker — even if its
          mutation checkpoint has already snapped to MAX. This is critical
          for the upgrade path: deployments running a prior version
          already have checkpoints from the buggy MAX-snap, but no marker,
          so they STILL recover their unindexed live files on the first
          start with this fix.
        - A handler that raises does NOT set its marker. The next daemon
          start retries the same set of unindexed paths for that consumer.

        Replay-from-zero is intentionally rejected — the issue (#4016) calls
        out the rename/delete churn and historical-replay cost. Synthesizing
        events from current ``file_paths`` state stays bounded by live data.
        """
        if self._async_session is None or not self._consumer_names:
            return

        handlers_by_name: dict[str, Any] = {
            "fts": self._consume_fts_mutations,
            "embedding": self._consume_embedding_mutations,
        }
        pending: list[tuple[str, Any]] = []
        for name in self._consumer_names:
            handler = handlers_by_name.get(name)
            if handler is None:
                continue
            if await self._reconciliation_completed(name):
                continue
            # Codex round-2: gate marker on the consumer's backend being
            # live. If the backend hasn't initialized yet (transient
            # startup failure), the handler would early-return without
            # work — marking it reconciled would permanently close the
            # recovery path on next restart.
            if not self._consumer_backend_ready(name):
                logger.info(
                    "Startup reconciliation: backend for %s not ready, "
                    "deferring — marker will not be set",
                    name,
                )
                continue
            pending.append((name, handler))

        if not pending:
            return

        events = await self._fetch_unindexed_path_events()
        if not events:
            # No unindexed live files — nothing to recover. Mark all
            # pending consumers reconciled so we don't re-scan on every
            # warm start.
            for name, _ in pending:
                await self._mark_reconciliation_completed(name)
            return

        logger.info(
            "Search startup reconciliation: replaying %d unindexed live "
            "file(s) for %d consumer(s) (%s).",
            len(events),
            len(pending),
            ",".join(name for name, _ in pending),
        )

        for name, handler in pending:
            try:
                await handler(events)
            except Exception as exc:
                logger.warning(
                    "Startup reconciliation handler %s failed: %s — "
                    "marker not set, will retry on next daemon start",
                    name,
                    exc,
                )
                continue
            await self._mark_reconciliation_completed(name)

    def _consumer_backend_ready(self, consumer_name: str) -> bool:
        """Whether ``consumer_name``'s backend is live enough to do real work.

        Mirrors the early-return guards in each ``_consume_*_mutations``
        handler. Needed by ``_reconcile_unindexed_paths_at_startup`` so
        we don't write a reconciliation marker for a consumer whose
        handler would silently no-op (Codex round-2).
        """
        if consumer_name == "fts":
            return self._chunk_store is not None
        if consumer_name == "embedding":
            return self._indexing_pipeline is not None and self._embedding_provider is not None
        return False

    async def _fetch_unindexed_path_events(self) -> list[SearchMutationEvent]:
        """Synthesize UPSERT events for live ``file_paths`` rows missing/stale chunks."""
        if self._async_session is None:
            return []

        async with self._async_session() as session:
            result = await session.execute(
                sa_text(
                    "SELECT zone_id, virtual_path, path_id "
                    "FROM file_paths "
                    "WHERE deleted_at IS NULL "
                    "AND (indexed_content_id IS NULL "
                    "OR indexed_content_id != content_id)"
                )
            )
            rows = result.fetchall()

        events: list[SearchMutationEvent] = []
        now = datetime.now(UTC).replace(tzinfo=None)
        for row in rows:
            zone_id = str(row[0])
            virtual_path = str(row[1])
            path_id = str(row[2])
            scoped_path = (
                virtual_path
                if virtual_path.startswith("/zone/")
                else f"/zone/{zone_id}{virtual_path}"
            )
            events.append(
                SearchMutationEvent(
                    event_id=f"reconcile:{path_id}",
                    operation_id=f"reconcile:{path_id}",
                    op=SearchMutationOp.UPSERT,
                    path=scoped_path,
                    zone_id=zone_id,
                    timestamp=now,
                    sequence_number=0,
                )
            )
        return events

    async def _save_consumer_checkpoint(self, consumer_name: str, sequence_number: int) -> None:
        self._consumer_last_sequence[consumer_name] = sequence_number
        await self._persist_checkpoint(consumer_name, sequence_number)

    async def _fetch_operation_log_events(
        self,
        after_sequence: int,
        *,
        limit: int,
    ) -> list[SearchMutationEvent]:
        if self._async_session is None:
            return []

        async with self._async_session() as session:
            result = await session.execute(
                sa_text(
                    "SELECT operation_id, operation_type, zone_id, path, new_path, "
                    "created_at, sequence_number, change_type "
                    "FROM operation_log "
                    "WHERE status = 'success' "
                    "AND sequence_number > :last_sequence "
                    "AND operation_type IN ('write', 'delete', 'rename') "
                    "ORDER BY sequence_number "
                    "LIMIT :limit"
                ),
                {
                    "last_sequence": after_sequence,
                    "limit": limit,
                },
            )
            rows = result.fetchall()

        events: list[SearchMutationEvent] = []
        for row in rows:
            event = SearchMutationEvent.from_operation_log_row(row)
            if event is not None:
                events.append(event)
        return events

    async def _fetch_shared_mutation_window(self) -> list[SearchMutationEvent]:
        if self._async_session is None or not self._consumer_names:
            return []

        now = time.monotonic()
        consumer_sequences = {
            name: self._consumer_last_sequence.get(name, 0) for name in self._consumer_names
        }
        min_sequence = min(consumer_sequences.values(), default=0)
        cache_is_fresh = (
            self._shared_mutation_events
            and self._shared_mutation_floor_sequence <= min_sequence
            and (now - self._shared_mutation_loaded_at) < 0.25
        )
        if cache_is_fresh:
            return self._shared_mutation_events

        async with self._shared_mutation_lock:
            now = time.monotonic()
            consumer_sequences = {
                name: self._consumer_last_sequence.get(name, 0) for name in self._consumer_names
            }
            min_sequence = min(consumer_sequences.values(), default=0)
            cache_is_fresh = (
                self._shared_mutation_events
                and self._shared_mutation_floor_sequence <= min_sequence
                and (now - self._shared_mutation_loaded_at) < 0.25
            )
            if cache_is_fresh:
                return self._shared_mutation_events

            page_size = self.config.mutation_batch_size * max(2, len(consumer_sequences))
            max_window_rows = page_size * 8
            remaining = dict.fromkeys(consumer_sequences.keys(), self.config.mutation_batch_size)
            cursor = min_sequence
            window: list[SearchMutationEvent] = []

            while True:
                page = await self._fetch_operation_log_events(cursor, limit=page_size)
                if not page:
                    break

                window.extend(page)
                for event in page:
                    for name, sequence in consumer_sequences.items():
                        if remaining[name] > 0 and event.sequence_number > sequence:
                            remaining[name] -= 1

                cursor = page[-1].sequence_number
                if all(count == 0 for count in remaining.values()):
                    break
                if len(page) < page_size or len(window) >= max_window_rows:
                    break

            self._shared_mutation_events = window
            self._shared_mutation_floor_sequence = min_sequence
            self._shared_mutation_loaded_at = now
            return window

    async def _fetch_mutation_events(
        self,
        consumer_name: str,
    ) -> list[SearchMutationEvent]:
        last_sequence = self._consumer_last_sequence.get(consumer_name)
        if last_sequence is None:
            last_sequence = await self._initialize_consumer_checkpoint(consumer_name)
            self._consumer_last_sequence[consumer_name] = last_sequence

        shared_window = await self._fetch_shared_mutation_window()
        events = [event for event in shared_window if event.sequence_number > last_sequence][
            : self.config.mutation_batch_size
        ]
        if events:
            return events

        return await self._fetch_operation_log_events(
            last_sequence,
            limit=self.config.mutation_batch_size,
        )

    async def _run_mutation_consumer(
        self,
        consumer_name: str,
        handler: Any,
    ) -> None:
        while not self._shutting_down:
            try:
                events = await self._fetch_mutation_events(consumer_name)
                if events:
                    await handler(events)
                    await self._save_consumer_checkpoint(consumer_name, events[-1].sequence_number)
                    self._consumer_failures[consumer_name] = 0
                    self._consumer_last_error[consumer_name] = None
                    self.stats.mutation_consumers[consumer_name] = {
                        "last_sequence": events[-1].sequence_number,
                        "last_success_at": time.time(),
                        "failures": 0,
                    }
                    self.stats.last_index_refresh = time.time()
                    continue

                self._mutation_wakeup.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._mutation_wakeup.wait(),
                        timeout=self.config.mutation_poll_seconds,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._consumer_failures[consumer_name] = (
                    self._consumer_failures.get(consumer_name, 0) + 1
                )
                self._consumer_last_error[consumer_name] = str(exc)
                self.stats.mutation_consumers[consumer_name] = {
                    "last_sequence": self._consumer_last_sequence.get(consumer_name, 0),
                    "last_success_at": self.stats.mutation_consumers.get(consumer_name, {}).get(
                        "last_success_at"
                    ),
                    "failures": self._consumer_failures[consumer_name],
                    "last_error": str(exc),
                }
                logger.warning("Search mutation consumer %s failed: %s", consumer_name, exc)
                await asyncio.sleep(self.config.mutation_poll_seconds)

    async def _resolve_mutations(
        self,
        events: list[SearchMutationEvent],
    ) -> list[ResolvedMutation]:
        if self._mutation_resolver is None:
            return []
        return await self._mutation_resolver.resolve_batch(events)

    def _collapse_resolved_mutations(
        self,
        resolved: list[ResolvedMutation],
    ) -> list[ResolvedMutation]:
        """Collapse multiple mutations for the same document within one batch.

        Shared op-log windows can surface several writes for the same document
        together. For downstream consumers, the last mutation in sequence order
        is the only one that matters.
        """
        seen: set[tuple[str, str]] = set()
        collapsed: list[ResolvedMutation] = []
        for mutation in reversed(resolved):
            key = (mutation.zone_id, mutation.doc_id)
            if key in seen:
                continue
            seen.add(key)
            collapsed.append(mutation)
        collapsed.reverse()
        return collapsed

    async def _consume_fts_mutations(self, events: list[SearchMutationEvent]) -> None:
        if self._chunk_store is None:
            return
        embedding_active = (
            self._indexing_pipeline is not None and self._embedding_provider is not None
        )
        resolved = self._collapse_resolved_mutations(await self._resolve_mutations(events))
        for mutation in resolved:
            # Codex review R10 #1 (high): refuse to checkpoint
            # unresolved-content UPSERTs — see commit history for rationale
            # for the rationale.
            if mutation.event.op == SearchMutationOp.UPSERT and not mutation.content_resolved:
                raise RuntimeError(
                    f"FTS mutation content unresolved for "
                    f"event_id={mutation.event.event_id} "
                    f"path={mutation.event.path} — refusing to checkpoint "
                    "so the consumer retries on next pass"
                )
            is_delete_shaped = mutation.event.op == SearchMutationOp.DELETE or (
                mutation.event.op == SearchMutationOp.UPSERT and mutation.content == ""
            )
            if is_delete_shaped:
                # When embedding consumer is active, it owns deletes for
                # document_chunks (Issue #3708). FTS only handles deletes
                # when it is the sole writer.
                if not embedding_active and self._has_resolved_path_id(mutation):
                    await self._chunk_store.delete_document_chunks(mutation.path_id)
                # Codex review R7 (high): always prune the SANDBOX vec
                # lane on DELETE here too. The embedding consumer's
                # prune (R6) only fires when an embedding provider is
                # wired (rare in current txtai-era wiring), so the FTS
                # consumer is the production carrier of deletes.
                #
                # Codex review R10 #2 (high): do NOT swallow vec delete
                # failures. The consumer checkpoints whenever this
                # handler returns successfully, so a swallowed failure
                # means deleted/renamed paths stay searchable in the
                # vector lane forever. Let exceptions propagate so the
                # batch is retried.
                if self._sqlite_vec_backend is not None and mutation.event.path:
                    # Codex review R9 #3 (high): prune BOTH the
                    # canonical (unscoped) key and the original
                    # event-path key so legacy scoped rows from
                    # pre-R9 builds also get cleaned up.
                    delete_keys = [mutation.virtual_path]
                    if mutation.event.path != mutation.virtual_path:
                        delete_keys.append(mutation.event.path)
                    await self._sqlite_vec_backend.delete(delete_keys, zone_id=mutation.zone_id)
                continue
            if mutation.content:
                if not self._has_resolved_path_id(mutation):
                    continue
                # When embedding is active, skip in-scope paths — the
                # embedding consumer writes strictly better chunks for
                # those. Keep FTS as the writer for out-of-scope paths
                # so they remain searchable via FTS (Issue #3708).
                #
                # NOTE: there is a narrow race window if scope changes
                # between this check and the embedding consumer's check
                # (both could skip). Scope changes are infrequent admin
                # ops, and the next mutation batch self-corrects. A
                # scope-generation token would close this gap if needed.
                if embedding_active and self._is_path_in_scope(mutation.event.path):
                    continue
                await self._index_to_document_chunks(
                    mutation.path_id,
                    mutation.event.path,
                    mutation.content,
                )

    async def _consume_embedding_mutations(self, events: list[SearchMutationEvent]) -> None:
        if self._indexing_pipeline is None or self._embedding_provider is None:
            return
        resolved = self._collapse_resolved_mutations(await self._resolve_mutations(events))
        for mutation in resolved:
            # Codex review R10 #1 (high): refuse to checkpoint
            # unresolved-content UPSERTs — see commit history for rationale
            # for the rationale.
            if mutation.event.op == SearchMutationOp.UPSERT and not mutation.content_resolved:
                raise RuntimeError(
                    f"Embedding mutation content unresolved for "
                    f"event_id={mutation.event.event_id} "
                    f"path={mutation.event.path} — refusing to checkpoint "
                    "so the consumer retries on next pass"
                )
            # Codex review R10 #1 (high): treat resolved-empty UPSERTs
            # as DELETEs (real truncation). Combined with the explicit
            # DELETE op below.
            is_delete_shaped = mutation.event.op == SearchMutationOp.DELETE or (
                mutation.event.op == SearchMutationOp.UPSERT and mutation.content == ""
            )
            if is_delete_shaped:
                if self._chunk_store is not None and self._has_resolved_path_id(mutation):
                    await self._chunk_store.delete_document_chunks(mutation.path_id)
                # Codex review R6 (high): also prune the SANDBOX vec
                # backend so deleted/renamed paths don't survive in
                # the hybrid vector lane. Renames arrive here as
                # DELETE-on-old-path (followed by an UPSERT on the new
                # path that the side-write in _bulk_insert will cover).
                #
                # Codex review R10 #2 (high): do NOT swallow vec delete
                # failures (see ``_consume_fts_mutations`` for the same
                # rationale).
                if self._sqlite_vec_backend is not None and mutation.event.path:
                    delete_keys = [mutation.virtual_path]
                    if mutation.event.path != mutation.virtual_path:
                        delete_keys.append(mutation.event.path)
                    await self._sqlite_vec_backend.delete(delete_keys, zone_id=mutation.zone_id)
                continue
            if not mutation.content:
                continue
            if not self._has_resolved_path_id(mutation):
                continue
            # Early-skip for out-of-scope paths (Issue #3698). The central
            # gate in IndexingPipeline.index_documents would also catch
            # this, but filtering here avoids the pipeline overhead.
            if not self._is_path_in_scope(mutation.event.path):
                continue
            # The pipeline can fail via IndexResult.error OR by raising
            # (e.g. provider/network failures). In both cases, fall back to
            # naive FTS chunks so the document stays searchable. If the
            # fallback also fails, re-raise so the batch is NOT checkpointed
            # and will be retried (Issue #3708).
            try:
                result = await self._indexing_pipeline.index_document(
                    mutation.event.path,
                    mutation.content,
                    mutation.path_id,
                )
                if not result.error:
                    continue
                error_detail = result.error
            except Exception as exc:
                error_detail = str(exc)

            logger.warning(
                "Embedding pipeline failed for %s: %s — falling back to FTS chunks",
                mutation.event.path,
                error_detail,
            )
            if self._chunk_store is not None:
                await self._chunk_store.replace_document_chunks(
                    mutation.path_id,
                    self._build_naive_chunks(mutation.content),
                )

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

        for path in paths:
            try:
                # Strip /zone/{zone_id} prefix for DB virtual_path lookup
                virtual_path = strip_zone_prefix(path)
                zone_id = extract_zone_id(path)
                lookup_candidates = self._path_lookup_candidates(zone_id, path, virtual_path)

                # Read file content via file reader or database
                content: str | None = None
                content_path_id: str | None = None
                if self._file_reader:
                    import contextlib

                    try:
                        content = await self._file_reader.read_text(path)
                    except Exception as e:
                        logger.debug("File read failed for %s: %s, trying virtual path", path, e)
                        # Also try without zone prefix — best-effort fallback
                        with contextlib.suppress(OSError, ValueError):
                            content = await self._file_reader.read_text(virtual_path)

                # Fallback: read from content_cache table
                if not content and self._async_session:
                    try:
                        async with self._async_session() as sess:
                            values_sql, params = self._build_path_lookup_values(lookup_candidates)
                            params["zone_id"] = zone_id
                            row = (
                                await sess.execute(
                                    sa_text(
                                        "WITH lookup(virtual_path, lookup_rank) AS "
                                        f"(VALUES {values_sql}) "
                                        "SELECT cc.content_text, fp.path_id FROM lookup l "
                                        "JOIN file_paths fp "
                                        "ON fp.zone_id = :zone_id "
                                        "AND fp.virtual_path = l.virtual_path "
                                        "JOIN content_cache cc ON cc.path_id = fp.path_id "
                                        "WHERE fp.deleted_at IS NULL "
                                        "AND cc.content_text IS NOT NULL "
                                        "ORDER BY l.lookup_rank "
                                        "LIMIT 1"
                                    ),
                                    params,
                                )
                            ).first()
                            if row and row[0]:
                                content = str(row[0])
                                if len(row) > 1 and row[1]:
                                    content_path_id = str(row[1])
                    except Exception as db_err:
                        logger.debug("DB content read failed for %s: %s", path, db_err)

                if not content:
                    logger.debug("No content found for %s", path)
                    continue

                # Scrub NUL bytes — PG TEXT rejects them (SQLSTATE 22021) and
                # this legacy refresh path bypasses MutationResolver's central
                # scrub (Issue #3989). Done once here so every downstream
                # consumer below (BM25S, embedding pipeline, naive FTS chunks)
                # sees clean content.
                if "\x00" in content:
                    content = content.replace("\x00", "")

                # Resolve path_id from file_paths table
                path_id = virtual_path  # fallback
                path_id_resolved = False
                if content_path_id is not None:
                    path_id = content_path_id
                    path_id_resolved = True
                elif self._async_session:
                    try:
                        async with self._async_session() as sess:
                            values_sql, params = self._build_path_lookup_values(lookup_candidates)
                            params["zone_id"] = zone_id
                            row = (
                                await sess.execute(
                                    sa_text(
                                        "WITH lookup(virtual_path, lookup_rank) AS "
                                        f"(VALUES {values_sql}) "
                                        "SELECT fp.path_id FROM lookup l "
                                        "JOIN file_paths fp "
                                        "ON fp.zone_id = :zone_id "
                                        "AND fp.virtual_path = l.virtual_path "
                                        "WHERE fp.deleted_at IS NULL "
                                        "ORDER BY l.lookup_rank "
                                        "LIMIT 1"
                                    ),
                                    params,
                                )
                            ).first()
                            if row:
                                path_id = row[0]
                                path_id_resolved = True
                    except Exception as e:
                        logger.debug("path_id lookup failed for %s: %s", virtual_path, e)

                # Single-writer policy for document_chunks (Issue #3708):
                # when embedding pipeline is active AND the path is in
                # scope, let the pipeline be the sole writer (semantic
                # chunks + embeddings). Only fall back to naive FTS chunks
                # when the pipeline is absent or the path is out of scope.
                path_in_scope = self._is_path_in_scope(path)
                embedding_active = (
                    self._indexing_pipeline is not None
                    and self._embedding_provider is not None
                    and path_in_scope
                )

                if embedding_active and self._indexing_pipeline is not None:
                    if path_id_resolved:
                        try:
                            result = await self._indexing_pipeline.index_document(
                                path, content, path_id
                            )
                            if result.error:
                                logger.warning(
                                    "Embedding pipeline failed for %s: %s — falling back to FTS",
                                    path,
                                    result.error,
                                )
                                await self._index_to_document_chunks(path_id, path, content)
                        except Exception as ie:
                            logger.warning("Indexing pipeline error for %s: %s", path, ie)
                            await self._index_to_document_chunks(path_id, path, content)
                elif self._async_session and path_id_resolved:
                    await self._index_to_document_chunks(path_id, path, content)

                indexed_count += 1

            except Exception as e:
                logger.warning("Failed to refresh index for %s: %s", path, e)

        # Issue #3699: txtai batch upsert is gone. Chunks + embeddings are
        # written by ChunkStore.replace_document_chunks via the indexing
        # pipeline above; the new search backends index from the same
        # ``document_chunks`` table automatically.

        if indexed_count > 0:
            logger.info("[DAEMON] Indexed %d/%d files", indexed_count, len(paths))

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
            # Issue #3699: search backends (replaced txtai).
            "backend": (
                type(self._fts_backend).__name__ if self._fts_backend is not None else "legacy"
            ),
            "vector_backend": (
                type(self._vector_backend).__name__ if self._vector_backend is not None else None
            ),
            # ``None`` means no embedding model configured — keyword-only mode.
            "embedding_model": getattr(self.config, "embedding_model", None)
            or getattr(self.config, "txtai_model", None),
            # Backward-compatible aliases for tooling that still reads the
            # txtai_* keys. txtai is gone (Issue #3699), but stats consumers
            # such as /search/stats dashboards may key off these names.
            "txtai_model": self.config.txtai_model,
            "txtai_reranker": self.config.txtai_reranker,
            "txtai_graph": self.config.txtai_graph,
            "mutation_consumers": self.stats.mutation_consumers,
            # Issue #3773: path-context attach runs fail-soft so search is
            # never broken by a context-lookup bug. Expose the counts so
            # operators can spot persistent failures via /search/stats.
            "path_context_attach_failures": self.stats.path_context_attach_failures,
            "path_context_resolve_failures": self.stats.path_context_resolve_failures,
        }

    def get_health(self) -> dict[str, Any]:
        """Get health status for health check endpoint.

        Returns:
            Health status dictionary
        """
        # Keyword search is available via Zoekt or DB FTS fallback
        keyword_ready = self.stats.zoekt_available or self._async_engine is not None
        return {
            "status": "healthy" if self._initialized else "starting",
            "initialized": self._initialized,
            "daemon_initialized": self._initialized,
            "backend": (
                type(self._fts_backend).__name__ if self._fts_backend is not None else "legacy"
            ),
            "bm25_index_loaded": keyword_ready,
            "db_pool_ready": self._async_engine is not None,
            "zoekt_available": self.stats.zoekt_available,
        }


async def create_and_start_daemon(
    database_url: str | None = None,
    *,
    async_session_factory: Any | None = None,
) -> SearchDaemon:
    """Create, configure and start a search daemon.

    Convenience function for creating a fully initialized daemon.

    Args:
        database_url: Database URL (from env if not provided)
        async_session_factory: Injected async_sessionmaker from RecordStoreABC.

    Returns:
        Initialized SearchDaemon instance
    """
    config = DaemonConfig(
        database_url=database_url or get_database_url(),
    )

    daemon = SearchDaemon(config, async_session_factory=async_session_factory)
    await daemon.startup()
    return daemon
