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
from nexus.lib.env import get_database_url

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from nexus.bricks.search.bm25s_search import BM25SIndex
    from nexus.bricks.search.chunking import EntropyAwareChunker
    from nexus.bricks.search.index_scope import IndexScope
    from nexus.bricks.search.indexing import IndexingPipeline
    from nexus.bricks.search.path_context import PathContextCache

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

    # BM25S settings
    bm25s_index_dir: str = ".nexus-data/bm25s"
    bm25s_mmap: bool = True  # Memory-mapped for instant loading

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

    # txtai backend config (Issue #2663)
    txtai_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    txtai_vectors: dict[str, Any] | None = None
    txtai_reranker: str | None = None  # e.g. "cross-encoder/ms-marco-MiniLM-L-2-v2"
    txtai_sparse: bool = False  # Enable SPLADE learned sparse retrieval
    # Semantic graph: disabled by default because txtai's graph upsert path
    # calls ``grand.backends._sqlbackend.add_edges_from`` which issues
    # ``INSERT INTO edges DEFAULT VALUES``, failing the NOT NULL constraint
    # on the ``ID`` column. That tears down the enclosing transaction and
    # drops every co-batched document write. The graph is only consumed by
    # the rarely-used ``graph_mode`` query parameter (low/high/dual/auto)
    # on ``/api/v2/search/query`` — default ``graph_mode=none`` does not
    # need it. Operators who want the feature can flip this in config.
    txtai_graph: bool = False  # Enable semantic graph (opt-in; see note above)

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
        self._path_context_cache_by_loop: dict[Any, Any] = {}
        # Engines we created for loop-local caches — tracked for disposal
        # on shutdown so pooled connections don't leak (Issue #3773 review).
        self._path_context_engines_by_loop: dict[Any, Any] = {}

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
        self._checkpoint_file = (
            Path(self.config.bm25s_index_dir).parent / "mutation-checkpoints.json"
        )
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

        # txtai backend (Issue #2663) — used for semantic/hybrid search + graph
        self._backend: Any = None
        self._txtai_bootstrap_task: asyncio.Task[None] | None = None
        self._txtai_bootstrapped = False
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

        Runs once from ``startup()`` before the txtai backend bootstrap
        and before the mutation consumers spin up so the very first
        refresh already respects scope.

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
                        if purged.get("txtai_docs", 0):
                            logger.info(
                                "scope refresh tick: zone %s shrank — "
                                "self-healed by purging %d stale txtai docs",
                                zid,
                                purged.get("txtai_docs", 0),
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

        # Load per-directory semantic index scope (Issue #3698) BEFORE
        # initializing the txtai backend or spawning the bootstrap task.
        # The bootstrap snapshots ``_zone_indexing_modes`` to decide whether
        # to push the SQL scope filter; if bootstrap races the scope load,
        # it sees an empty mode map, takes the legacy fast path, and
        # replays every document_chunks row (including out-of-scope ones)
        # into the txtai backend. Loading scope first is the only
        # reliable way to prevent that leak across restarts. Must run
        # synchronously — do NOT kick off as a background task.
        await self._load_index_scope()

        # Initialize txtai backend for semantic/hybrid/graph search (Issue #2663)
        try:
            from nexus.bricks.search.txtai_backend import TxtaiBackend

            # Pass embedding cache so txtai can skip redundant API calls
            _emb_cache = None
            if self._cache_brick:
                import contextlib

                with contextlib.suppress(Exception):
                    _emb_cache = self._cache_brick.embedding_cache

            self._backend = TxtaiBackend(
                database_url=self.config.database_url,
                model=self.config.txtai_model,
                vectors=self.config.txtai_vectors,
                hybrid=True,
                graph=self.config.txtai_graph,
                reranker_model=self.config.txtai_reranker,
                sparse=self.config.txtai_sparse,
                embedding_cache=_emb_cache,
                data_path=self.config.data_path if hasattr(self.config, "data_path") else None,
            )
            self._backend.kickoff_startup()
            self._txtai_bootstrap_task = asyncio.create_task(self._bootstrap_txtai_backend())
            logger.info("txtai backend startup kicked off in background")
        except Exception:
            logger.warning(
                "txtai backend init failed, falling back to legacy search", exc_info=True
            )
            self._backend = None
            self._txtai_bootstrapped = False

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
        for task in self._consumer_tasks.values():
            task.cancel()
        if self._txtai_bootstrap_task and not self._txtai_bootstrap_task.done():
            self._txtai_bootstrap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._txtai_bootstrap_task
        self._txtai_bootstrap_task = None

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

        # Shutdown txtai backend (Issue #2663)
        if self._backend is not None:
            try:
                await self._backend.shutdown()
            except Exception as e:
                logger.debug("txtai backend shutdown error: %s", e)
            self._backend = None
        self._txtai_bootstrapped = False

        # Close database connections (only if we created them)
        if self._owns_engine:
            if self._record_store is not None:
                self._record_store.close()
                self._record_store = None
            elif self._async_engine:
                await self._async_engine.dispose()
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

    # PersistentService protocol aliases
    start = startup
    stop = shutdown

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
            await self.locate("warmup", zone_id="root", limit=1)
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
        stale = [lk for lk in self._path_context_cache_by_loop if lk.is_closed()]
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
        Many backend paths (txtai, legacy BM25) construct ``SearchResult``
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

        try:
            # For keyword search: Zoekt first (code search), then txtai BM25.
            # Zoekt has no zone metadata — only safe for root zone / unscoped.
            is_zone_safe = effective_zone_id == ROOT_ZONE_ID
            if search_type == "keyword" and self.stats.zoekt_available and is_zone_safe:
                zoekt_results = await self._search_zoekt(query, limit, path_filter)
                if zoekt_results:
                    latency_ms = (time.perf_counter() - start) * 1000
                    self._track_latency(latency_ms)
                    self.last_search_timing["backend_ms"] = latency_ms
                    await self._attach_path_contexts(zoekt_results, zone_id=effective_zone_id)
                    return zoekt_results

            # Delegate to txtai backend for all search types (Issue #2663)
            if self._backend is not None and (
                self._txtai_bootstrapped or self._txtai_bootstrap_task is None
            ):
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
                    await self._attach_path_contexts(results, zone_id=effective_zone_id)
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

            await self._attach_path_contexts(results, zone_id=effective_zone_id)
            return results

        except TimeoutError:
            logger.warning(f"Search timeout after {self.config.query_timeout_seconds}s")
            return []

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
        if self._backend is None:
            return [[] for _ in queries]

        backend_batch = getattr(self._backend, "batch_search", None)
        if backend_batch is None:
            return [[] for _ in queries]

        results: list[list[Any]] = await backend_batch(queries, zone_id=effective_zone_id)
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

    async def _bootstrap_txtai_backend(self) -> None:
        """Populate txtai from canonical SQL chunks so restarts keep semantic search.

        Per-directory semantic index scoping (Issue #3698): zones in
        ``'scoped'`` mode only replay chunks whose ``virtual_path`` falls
        under a registered ``indexed_directories`` row. The filter is
        pushed into SQL so we never materialize out-of-scope rows in
        Python memory — critical for large workspaces.

        Issue #3704: uses keyset-paginated ``session.execute()`` so the
        read cursor is closed **before** each call to ``_backend.upsert()``.
        On PostgreSQL this prevents the long-lived read snapshot that the
        old ``session.stream()`` approach caused, which blocked autovacuum
        on ``document_chunks`` / ``file_paths`` during large restarts.

        Each page selects at most ``_PAGE_FILES`` complete files via a
        subquery, guaranteeing no file is split across pages.  Within each
        page a running-accumulator assembles chunks; a per-doc cap
        (``_MAX_CHUNKS_PER_DOC``) prevents one pathological file from
        spiking transient memory.
        """
        if self._backend is None or self._async_session is None:
            self._txtai_bootstrapped = self._backend is None
            return

        from sqlalchemy import bindparam
        from sqlalchemy import text as sa_text

        # Distinct files fetched per DB round-trip.  Each page is a
        # bounded ``fetchall()``; the cursor closes before any upsert.
        _PAGE_FILES = 200

        # Per-document chunk truncation cap.  Files with more chunks than
        # this are indexed with only their first _MAX_CHUNKS_PER_DOC chunks.
        # Prevents a single giant JSONL/log file from spiking transient
        # allocation during the join + "\n".join() step.
        _MAX_CHUNKS_PER_DOC = 500

        # Maximum assembled docs buffered per zone before flushing to txtai.
        _UPSERT_BATCH = 200

        try:
            scoped_zone_ids = sorted(
                zid for zid, mode in self._zone_indexing_modes.items() if mode == "scoped"
            )

            # Build the per-page query.  The inner subquery selects the next
            # _PAGE_FILES distinct (zone_id, virtual_path) pairs after the
            # keyset (kz, kp), guaranteeing that every file in the outer
            # result is complete — no file spans a page boundary.
            if not scoped_zone_ids:
                # Fast path: no scope filter.
                page_stmt = sa_text(
                    """
                    SELECT fp.zone_id, fp.virtual_path, c.chunk_index, c.chunk_text
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE fp.deleted_at IS NULL
                      AND fp.path_id IN (
                          SELECT path_id FROM file_paths
                          WHERE deleted_at IS NULL
                            AND (zone_id > :kz OR (zone_id = :kz AND virtual_path > :kp))
                          ORDER BY zone_id, virtual_path
                          LIMIT :n_files
                      )
                    ORDER BY fp.zone_id, fp.virtual_path, c.chunk_index
                    """
                )
                base_params: dict[str, Any] = {}
            else:
                # Scoped path: inner subquery applies the same scope filter
                # so out-of-scope files are excluded from the page count.
                #
                # WILDCARD ESCAPING: ``LIKE`` interprets ``_`` and ``%``
                # as wildcards. A directory like ``/team_a`` would
                # otherwise match ``/teamXa/foo`` and reintroduce
                # out-of-scope rows after restart. We escape the pattern
                # via nested REPLACE() so the match is a literal prefix
                # only, with ``ESCAPE '\'`` honoured by both Postgres and
                # SQLite.
                page_stmt = sa_text(
                    r"""
                    SELECT fp.zone_id, fp.virtual_path, c.chunk_index, c.chunk_text
                    FROM document_chunks c
                    JOIN file_paths fp ON c.path_id = fp.path_id
                    WHERE fp.deleted_at IS NULL
                      AND fp.path_id IN (
                          SELECT fp2.path_id FROM file_paths fp2
                          WHERE fp2.deleted_at IS NULL
                            AND (fp2.zone_id > :kz OR (fp2.zone_id = :kz AND fp2.virtual_path > :kp))
                            AND (
                              fp2.zone_id NOT IN :scoped_zones
                              OR EXISTS (
                                  SELECT 1 FROM indexed_directories d
                                  WHERE d.zone_id = fp2.zone_id
                                    AND (
                                      d.directory_path = '/'
                                      OR fp2.virtual_path = d.directory_path
                                      OR fp2.virtual_path LIKE
                                          REPLACE(
                                            REPLACE(
                                              REPLACE(d.directory_path, '\', '\\'),
                                              '%', '\%'
                                            ),
                                            '_', '\_'
                                          ) || '/%' ESCAPE '\'
                                    )
                              )
                            )
                          ORDER BY fp2.zone_id, fp2.virtual_path
                          LIMIT :n_files
                      )
                    ORDER BY fp.zone_id, fp.virtual_path, c.chunk_index
                    """
                ).bindparams(
                    # expanding=True lets SQLAlchemy render an IN-list
                    # from a Python sequence for both Postgres and SQLite.
                    bindparam("scoped_zones", expanding=True),
                )
                base_params = {"scoped_zones": scoped_zone_ids}

            total = 0
            kz: str = ""  # keyset zone  — '' sorts before all real zone_ids
            kp: str = ""  # keyset path  — '' sorts before all real paths

            while True:
                page_params = {**base_params, "kz": kz, "kp": kp, "n_files": _PAGE_FILES}

                # --- Read phase: cursor opens and closes here ---
                async with self._async_session() as session:
                    result = await session.execute(page_stmt, page_params)
                    rows = result.fetchall()
                # Cursor is fully released before any upsert call.

                if not rows:
                    break

                # --- Assemble + Upsert phase (no DB connection held) ---
                cur_zone: str | None = None
                cur_path: str | None = None
                cur_chunks: list[str] = []
                docs_batch: dict[str, list[dict[str, Any]]] = {}

                for row in rows:
                    zone = row.zone_id or "root"
                    path = row.virtual_path

                    if (zone, path) != (cur_zone, cur_path):
                        # Completed document — push to batch.
                        if cur_path is not None and cur_zone is not None and cur_chunks:
                            content = "\n".join(c for c in cur_chunks if c)
                            if content.strip():
                                doc_id = (
                                    f"{cur_zone}:{cur_path}" if cur_zone != "root" else cur_path
                                )
                                docs_batch.setdefault(cur_zone, []).append(
                                    {
                                        "id": doc_id,
                                        "text": content,
                                        "path": cur_path,
                                        "zone_id": cur_zone,
                                    }
                                )
                                # Mid-page flush: only cur_zone just grew, so
                                # check only that zone — O(1) not O(zones).
                                if len(docs_batch[cur_zone]) >= _UPSERT_BATCH:
                                    total += int(
                                        await self._backend.upsert(
                                            docs_batch.pop(cur_zone), zone_id=cur_zone
                                        )
                                    )

                        # Zone boundary: flush the completed zone immediately.
                        # ORDER BY ensures zones are monotonically non-decreasing
                        # within a page, so no rows for cur_zone can appear later.
                        if cur_zone is not None and zone != cur_zone:
                            zone_docs = docs_batch.pop(cur_zone, [])
                            if zone_docs:
                                total += int(
                                    await self._backend.upsert(zone_docs, zone_id=cur_zone)
                                )

                        cur_zone = zone
                        cur_path = path
                        cur_chunks = []

                    # Per-doc chunk cap: silently truncate pathological files
                    # so one huge file cannot spike transient memory.
                    if len(cur_chunks) < _MAX_CHUNKS_PER_DOC:
                        cur_chunks.append(row.chunk_text or "")

                # Flush the final in-flight document.
                if cur_path is not None and cur_zone is not None and cur_chunks:
                    content = "\n".join(c for c in cur_chunks if c)
                    if content.strip():
                        doc_id = f"{cur_zone}:{cur_path}" if cur_zone != "root" else cur_path
                        docs_batch.setdefault(cur_zone, []).append(
                            {
                                "id": doc_id,
                                "text": content,
                                "path": cur_path,
                                "zone_id": cur_zone,
                            }
                        )

                # Flush all remaining zones for this page.
                for zid, docs in list(docs_batch.items()):
                    if docs:
                        total += int(await self._backend.upsert(docs, zone_id=zid))

                # Advance keyset cursor using raw DB values — must not
                # normalise zone_id here because the SQL predicate compares
                # against the raw column values.  Normalising "" → "root"
                # would break the cursor on deployments with legacy empty-string
                # zone_ids and cause infinite re-fetching of those rows.
                kz = rows[-1].zone_id
                kp = rows[-1].virtual_path

            self._txtai_bootstrapped = True
            if total:
                self.stats.last_index_refresh = time.time()
            logger.info("txtai bootstrap indexed %d existing documents", total)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._txtai_bootstrapped = False
            logger.warning("txtai bootstrap failed, continuing with legacy search: %s", exc)

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

        # Fall back to BM25S (in-memory, very fast).
        if self._bm25s_index and is_zone_safe:
            bm25s_results = await self._search_bm25s(query, limit, path_filter)
            if bm25s_results:
                return bm25s_results

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
            "bm25": self._consume_bm25_mutations,
            "fts": self._consume_fts_mutations,
            "embedding": self._consume_embedding_mutations,
            "txtai": self._consume_txtai_mutations,
        }
        self._consumer_names = tuple(consumer_specs.keys())
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
        if not resolved:
            return

        if self._chunk_store is not None:
            for mutation in resolved:
                if mutation.path_id != mutation.virtual_path:
                    await self._chunk_store.delete_document_chunks(mutation.path_id)

        if self._backend is not None:
            deletes_by_zone: dict[str, list[str]] = {}
            for mutation in resolved:
                deletes_by_zone.setdefault(mutation.zone_id, []).append(mutation.doc_id)
            for zone_id, ids in deletes_by_zone.items():
                await self._backend.delete(ids, zone_id=zone_id)

        if self._bm25s_index is not None and hasattr(self._bm25s_index, "delete_document"):
            for mutation in resolved:
                with contextlib.suppress(Exception):
                    await self._bm25s_index.delete_document(mutation.path_id)

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

    async def _consume_bm25_mutations(self, events: list[SearchMutationEvent]) -> None:
        if self._bm25s_index is None:
            return
        resolved = self._collapse_resolved_mutations(await self._resolve_mutations(events))
        for mutation in resolved:
            if mutation.event.op != SearchMutationOp.UPSERT or not mutation.content:
                continue
            await self._bm25s_index.index_document(
                mutation.path_id,
                mutation.event.path,
                mutation.content,
            )

    async def _consume_fts_mutations(self, events: list[SearchMutationEvent]) -> None:
        if self._chunk_store is None:
            return
        embedding_active = (
            self._indexing_pipeline is not None and self._embedding_provider is not None
        )
        resolved = self._collapse_resolved_mutations(await self._resolve_mutations(events))
        for mutation in resolved:
            if mutation.event.op == SearchMutationOp.DELETE:
                # When embedding consumer is active, it owns deletes for
                # document_chunks (Issue #3708). FTS only handles deletes
                # when it is the sole writer.
                if not embedding_active:
                    await self._chunk_store.delete_document_chunks(mutation.path_id)
                continue
            if mutation.content:
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
            # Handle deletes — the embedding consumer is now the sole
            # document_chunks writer when active (Issue #3708), so it must
            # propagate DELETE ops that the FTS consumer used to handle.
            if mutation.event.op == SearchMutationOp.DELETE:
                if self._chunk_store is not None:
                    await self._chunk_store.delete_document_chunks(mutation.path_id)
                continue
            if not mutation.content:
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

    async def _consume_txtai_mutations(self, events: list[SearchMutationEvent]) -> None:
        if self._backend is None:
            return

        resolved = self._collapse_resolved_mutations(await self._resolve_mutations(events))
        deletes_by_zone: dict[str, list[str]] = {}
        upserts_by_zone: dict[str, list[dict[str, Any]]] = {}

        for mutation in resolved:
            if mutation.event.op == SearchMutationOp.DELETE:
                # Deletes are NEVER filtered by scope (Issue #3698) — a
                # file that was in scope when first indexed must still
                # be cleaned up if it's later deleted, even if it now
                # falls outside the scope definition.
                deletes_by_zone.setdefault(mutation.zone_id, []).append(mutation.doc_id)
                continue
            if mutation.content:
                # Scope-filter upserts (Issue #3698). The helper takes the
                # raw event path; it handles zone extraction internally.
                if not self._is_path_in_scope(mutation.event.path):
                    continue
                upserts_by_zone.setdefault(mutation.zone_id, []).append(
                    {
                        "id": mutation.doc_id,
                        "text": mutation.content,
                        "path": mutation.virtual_path,
                        "zone_id": mutation.zone_id,
                    }
                )

        for zone_id, ids in deletes_by_zone.items():
            await self._backend.delete(ids, zone_id=zone_id)
        for zone_id, docs in upserts_by_zone.items():
            await self._backend.upsert(docs, zone_id=zone_id)

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
                virtual_path = strip_zone_prefix(path)

                # Read file content via file reader or database
                content: str | None = None
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
                elif self._async_session:
                    await self._index_to_document_chunks(path_id, path, content)

                indexed_count += 1

                # Collect for batched txtai upsert (Issue #2663)
                if self._backend is not None and path_in_scope:
                    # Fixed DRY bug (Issue #3698): use the shared
                    # extract_zone_id helper instead of an inline regex.
                    _zone = extract_zone_id(path)
                    _doc_id = f"{_zone}:{virtual_path}" if _zone != "root" else virtual_path
                    _txtai_batch.setdefault(_zone, []).append(
                        {
                            "id": _doc_id,
                            "text": content,
                            "path": virtual_path,
                            "zone_id": _zone,
                        }
                    )

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
                where_clauses = []
                params: dict[str, str] = {}
                for idx, vpath in enumerate(batch_vpaths):
                    where_clauses.append(f"virtual_path = :vpath_{idx}")
                    params[f"vpath_{idx}"] = vpath
                result = await session.execute(
                    text(
                        "SELECT path_id, virtual_path FROM file_paths "
                        "WHERE deleted_at IS NULL AND (" + " OR ".join(where_clauses) + ")"
                    ),
                    params,
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
