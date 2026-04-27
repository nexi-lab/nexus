"""Search startup: txtai-backed Search Daemon (Issue #2663).

Extracted from fastapi_server.py (#1602).
Rewritten for txtai backend (#2663).
"""

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes")


def _resolve_txtai_runtime_config() -> tuple[str, dict[str, str] | None]:
    """Resolve txtai embedding model and optional vectors config from env.

    Supports an explicit API-backed mode for Docker/demo stacks so users can
    avoid local embedding model startup when they already have OpenAI creds.
    """
    use_api_embeddings = _env_truthy("NEXUS_TXTAI_USE_API_EMBEDDINGS")
    configured_model = os.environ.get("NEXUS_TXTAI_MODEL", "").strip()
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    model = configured_model or "sentence-transformers/all-MiniLM-L6-v2"
    vectors: dict[str, str] | None = None

    if use_api_embeddings:
        if not configured_model and openai_api_key:
            model = "openai/text-embedding-3-small"

        if model.startswith("openai/") and openai_api_key:
            vectors = {"api_key": openai_api_key}
            if openai_base_url:
                vectors["api_base"] = openai_base_url

    return model, vectors or None


async def startup_search(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Initialize search daemon and return background tasks."""
    _search_env = os.getenv("NEXUS_SEARCH_DAEMON", "").lower()
    _explicit_off = _search_env in ("false", "0", "no")
    _explicit_on = _search_env in ("true", "1", "yes")
    # Default: auto-enable when a database URL is available (txtai requires postgres
    # for the BM25+vector pipeline). Explicit NEXUS_SEARCH_DAEMON=true forces it on
    # even without a database URL (e.g. SQLite-backed dev setups).
    search_daemon_enabled = _explicit_on or (not _explicit_off and bool(svc.database_url))

    if not search_daemon_enabled:
        logger.debug("Search Daemon disabled (set NEXUS_SEARCH_DAEMON=true to enable)")
        return []

    try:
        from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

        txtai_model, txtai_vectors = _resolve_txtai_runtime_config()
        _path_ctx_max_zones_env = os.environ.get("NEXUS_PATH_CONTEXT_MAX_ZONES")
        _path_ctx_max_zones = 2048
        if _path_ctx_max_zones_env:
            try:
                _path_ctx_max_zones = max(1, int(_path_ctx_max_zones_env))
            except ValueError:
                logger.warning(
                    "Invalid NEXUS_PATH_CONTEXT_MAX_ZONES=%r — falling back to 2048",
                    _path_ctx_max_zones_env,
                )
        config = DaemonConfig(
            database_url=svc.database_url,
            query_timeout_seconds=float(os.environ.get("NEXUS_QUERY_TIMEOUT", "10.0")),
            path_context_max_zones=_path_ctx_max_zones,
            # txtai backend config (Issue #2663)
            txtai_model=txtai_model,
            txtai_vectors=txtai_vectors,
            txtai_reranker=os.environ.get("NEXUS_TXTAI_RERANKER") or None,
            txtai_sparse=os.environ.get("NEXUS_TXTAI_SPARSE", "").lower() in ("true", "1", "yes"),
            # Semantic graph is off by default — txtai's graph upsert path
            # trips a pre-existing NotNullViolation in grand's edges table
            # that drops every co-batched document write. Operators who
            # want the rarely-used ``graph_mode`` query parameter can set
            # NEXUS_TXTAI_GRAPH=true to re-enable (at their own risk).
            txtai_graph=os.environ.get("NEXUS_TXTAI_GRAPH", "false").lower()
            in ("true", "1", "yes"),
        )

        # Inject async_session_factory from RecordStoreABC when available
        _record_store = svc.record_store
        _async_sf = None
        if _record_store is not None:
            with contextlib.suppress(AttributeError):
                _async_sf = _record_store.async_session_factory
        _settings_store = None
        with contextlib.suppress(ImportError, AttributeError):
            from nexus.storage.auth_stores.metastore_settings_store import MetastoreSettingsStore

            _settings_store = MetastoreSettingsStore(svc.nexus_fs.metadata)

        # Issue #2188: Create ZoektClient + embedding provider via DI
        _zoekt_client = None
        _search_cfg = None
        with contextlib.suppress(ImportError):
            from nexus.bricks.search.config import search_config_from_env
            from nexus.bricks.search.zoekt_client import ZoektClient

            _search_cfg = search_config_from_env()
            if _search_cfg.zoekt_enabled:
                _zoekt_client = ZoektClient(
                    base_url=_search_cfg.zoekt_url,
                    timeout=_search_cfg.zoekt_timeout,
                    enabled=True,
                )

        # CacheBrick is available from startup_permissions
        _cache_brick = getattr(app.state, "cache_brick", None)

        # Issue #3773: path context store + cache
        path_context_store = None
        path_context_cache = None
        if _async_sf is not None:
            try:
                from nexus.bricks.search.path_context import (
                    PathContextCache,
                    PathContextStore,
                )

                _db_type = (
                    "postgresql"
                    if (svc.database_url or "").startswith(("postgres", "postgresql"))
                    else "sqlite"
                )
                path_context_store = PathContextStore(
                    async_session_factory=_async_sf,
                    db_type=_db_type,
                )
                path_context_cache = PathContextCache(
                    store=path_context_store,
                    max_zones=config.path_context_max_zones,
                )
            except Exception:  # pragma: no cover — non-fatal wiring failure
                logger.exception("Failed to initialize path context store/cache")
        app.state.path_context_store = path_context_store
        app.state.path_context_cache = path_context_cache
        # Expose the database URL for loop-local resolvers that need to rebuild
        # engines on the request loop (Issue #3773 review feedback): the env
        # var may not be set when the app is constructed via
        # ``create_app(database_url=...)``.
        app.state.database_url = svc.database_url

        app.state.search_daemon = SearchDaemon(
            config,
            async_session_factory=_async_sf,
            zoekt_client=_zoekt_client,
            cache_brick=_cache_brick,
            settings_store=_settings_store,
            path_context_cache=path_context_cache,  # Issue #3773
        )

        # Embeddings are now handled by txtai backend (Issue #2663).
        # The old nexus.bricks.search.embeddings module has been deleted.

        # Do NOT register via sys_setattr — the Rust service_start_all() calls
        # startup() via asyncio.run(), which raises RuntimeError inside FastAPI's
        # running event loop. Start the daemon directly instead.
        await app.state.search_daemon.startup()
        app.state.search_daemon_enabled = True

        # Issue #1520: Set FileReaderProtocol for index refresh
        with contextlib.suppress(ImportError, AttributeError):
            from nexus.factory import _NexusFSFileReader

            # Thread parse_fn through so parseable binaries (.pdf, …) are
            # decoded into markdown text before the index refresh loop reads
            # them — otherwise the daemon indexes utf-8 garbage.
            from nexus.factory._semantic_search import _resolve_parse_fn

            _nxfs = svc.nexus_fs
            _pf = _resolve_parse_fn(_nxfs)
            app.state.search_daemon._file_reader = _NexusFSFileReader(_nxfs, parse_fn=_pf)
            if getattr(app.state.search_daemon, "_mutation_resolver", None) is not None:
                app.state.search_daemon._mutation_resolver.set_file_reader(  # noqa: SLF001
                    app.state.search_daemon._file_reader
                )

        # Wire SearchDaemon into SearchService so semantic_search queries
        # use the txtai backend instead of falling back to SQL ILIKE.
        with contextlib.suppress(AttributeError):
            search_svc = svc.nexus_fs.service("search")
            if search_svc is not None:
                search_svc._search_daemon = app.state.search_daemon

        # Auto-index on write/delete/rename: register VFS hooks that notify
        # the search daemon so the index stays fresh automatically.
        # NexusFS exposes register_intercept_write/delete/rename/copy directly.
        with contextlib.suppress(AttributeError, ImportError):
            _daemon_ref = app.state.search_daemon
            _nexus_fs = svc.nexus_fs
            if _nexus_fs is not None and hasattr(_nexus_fs, "register_intercept_write"):
                import asyncio as _asyncio

                from nexus.contracts.vfs_hooks import (
                    CopyHookContext,
                    DeleteHookContext,
                    RenameHookContext,
                    WriteHookContext,
                )

                # Capture the event loop at registration time — VFS hooks fire from
                # synchronous threads (asyncio.to_thread), so get_running_loop()
                # would raise RuntimeError. call_soon_threadsafe is thread-safe.
                _loop = _asyncio.get_running_loop()

                def _notify(path: str, change_type: str) -> None:
                    with contextlib.suppress(RuntimeError):  # Loop closed during shutdown
                        _loop.call_soon_threadsafe(
                            _loop.create_task,
                            _daemon_ref.notify_file_change(path, change_type),
                        )

                class _SearchWriteHook:
                    @property
                    def name(self) -> str:
                        return "search_auto_index"

                    def on_post_write(self, ctx: WriteHookContext) -> None:
                        _notify(ctx.path, "update")

                class _SearchDeleteHook:
                    @property
                    def name(self) -> str:
                        return "search_auto_delete"

                    def on_post_delete(self, ctx: DeleteHookContext) -> None:
                        _notify(ctx.path, "delete")

                class _SearchRenameHook:
                    @property
                    def name(self) -> str:
                        return "search_auto_rename"

                    def on_post_rename(self, ctx: RenameHookContext) -> None:
                        _notify(ctx.old_path, "delete")
                        _notify(ctx.new_path, "update")

                class _SearchCopyHook:
                    @property
                    def name(self) -> str:
                        return "search_auto_copy"

                    def on_post_copy(self, ctx: CopyHookContext) -> None:
                        _notify(ctx.dst_path, "update")

                _nexus_fs.register_intercept_write(_SearchWriteHook())
                _nexus_fs.register_intercept_delete(_SearchDeleteHook())
                _nexus_fs.register_intercept_rename(_SearchRenameHook())
                _nexus_fs.register_intercept_copy(_SearchCopyHook())
                logger.info("Search auto-index hooks registered (write/delete/rename/copy)")

        stats = app.state.search_daemon.get_stats()
        logger.info(
            "Search Daemon started: backend=%s, startup=%.1fms",
            stats.get("backend", "txtai"),
            stats["startup_time_ms"],
        )

        # Issue #3725: Wire SkeletonIndexer for live path+title index updates.
        # Creates the indexer (FileReaderProtocol + SkeletonBM25Protocol) and
        # registers VFS post-hooks so every write/delete/rename automatically
        # keeps the in-memory skeleton index fresh without a full re-bootstrap.
        await _wire_skeleton_indexer(app, svc)

        # Issue #3147: Initialize ZoneSearchRegistry for federated search.
        # Phase 1: All zones use the single global daemon.
        # Phase 2: Per-zone daemons can be registered if ZoneManager is available.
        _init_zone_registry(app, svc)

    except Exception as e:
        logger.warning("Failed to start Search Daemon: %s", e)
        app.state.search_daemon_enabled = False

    return []


async def shutdown_search(app: "FastAPI", svc: "LifespanServices") -> None:  # noqa: ARG001
    """Shut down the search daemon and release its resources."""
    daemon = getattr(app.state, "search_daemon", None)
    if daemon is None:
        return
    try:
        await daemon.shutdown()
        app.state.search_daemon_enabled = False
        logger.info("Search Daemon stopped")
    except Exception:
        app.state.search_daemon_enabled = False
        logger.warning("Search Daemon shutdown encountered errors", exc_info=True)


def _init_zone_registry(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize ZoneSearchRegistry with per-zone daemons (Issue #3147).

    Phase 1: Creates registry with the global daemon as default.
             All zones share this daemon — zone isolation via SQL WHERE.
    Phase 2: If ZoneManager is available, registers each known zone
             with capability detection from the daemon's stats.
    """
    from nexus.bricks.search.zone_registry import ZoneSearchCapabilities, ZoneSearchRegistry

    daemon = app.state.search_daemon
    registry = ZoneSearchRegistry(default_daemon=daemon)

    # Phase 2: Register per-zone capabilities if ZoneManager is available.
    # Each zone still uses the shared daemon (same DB), but gets its own
    # capabilities record so the dispatcher can make routing decisions.
    zone_manager = getattr(svc, "zone_manager", None)
    if zone_manager is not None:
        try:
            zone_ids = zone_manager.list_zones()
            # R20.12: capabilities persist to `{base_path}/{zone_id}/search_caps.json`.
            # Rust GetSearchCapabilities gRPC handler reads this file per RPC.
            base_path = getattr(zone_manager, "_base_path", None)
            for zone_id in zone_ids:
                caps = ZoneSearchCapabilities.from_daemon_stats(zone_id, daemon)
                registry.register(zone_id, daemon, capabilities=caps)
                if base_path is not None:
                    _write_search_caps_file(base_path, zone_id, caps)
            logger.info(
                "[ZONE-REGISTRY] Registered %d zones from ZoneManager",
                len(zone_ids),
            )
        except Exception as e:
            logger.warning("[ZONE-REGISTRY] Failed to register zones: %s", e)

    app.state.zone_search_registry = registry


def _write_search_caps_file(base_path: str, zone_id: str, caps: object) -> None:
    """Write per-zone search capabilities JSON (R20.12).

    Atomic: writes to `search_caps.json.tmp` then renames. Non-fatal on error
    — federation GetSearchCapabilities falls back to keyword-only defaults.
    """
    import json
    import os
    from pathlib import Path

    try:
        zone_dir = Path(base_path) / zone_id
        zone_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_tier": getattr(caps, "device_tier", "server"),
            "search_modes": list(getattr(caps, "search_modes", ["keyword"])),
            "embedding_model": getattr(caps, "embedding_model", None) or "",
            "embedding_dimensions": int(getattr(caps, "embedding_dimensions", 0) or 0),
            "has_graph": bool(getattr(caps, "has_graph", False)),
        }
        final_path = zone_dir / "search_caps.json"
        tmp_path = zone_dir / "search_caps.json.tmp"
        tmp_path.write_text(json.dumps(payload, indent=2))
        os.replace(tmp_path, final_path)
    except Exception as e:
        logger.warning("[ZONE-REGISTRY] Failed to write search_caps for %s: %s", zone_id, e)


async def _wire_skeleton_indexer(app: "FastAPI", svc: "LifespanServices") -> None:
    """Create SkeletonIndexer + SkeletonPipeConsumer, register VFS hooks.

    Called after SearchDaemon.startup() so the daemon's in-memory index is
    ready to receive upsert/delete calls via _DaemonSkeletonBM25.

    VFS hooks call SkeletonPipeConsumer.notify_* (sync, deque-buffered) rather
    than scheduling coroutines directly — the consumer's flush task drains the
    deque via the DT_PIPE so events are debounced and micro-batched (15A).

    Issue #3725 review decisions honoured:
        - 4A  Async pipe consumer (write/delete/rename routed via DT_PIPE).
        - 7A  2KB head cap enforced inside SkeletonIndexer.
        - 14A Hash-based skip guard in index_file().
        - 15A Micro-batched concurrent reads via asyncio.gather in consumer.
    """
    _nx = svc.nexus_fs
    _daemon = getattr(app.state, "search_daemon", None)
    if _nx is None or _daemon is None:
        return

    try:
        from nexus.bricks.catalog.extractors import SKELETON_EXTRACTOR_REGISTRY
        from nexus.bricks.search.skeleton_indexer import SkeletonIndexer
        from nexus.bricks.search.skeleton_pipe_consumer import SkeletonPipeConsumer
        from nexus.factory.adapters import _DaemonSkeletonBM25, _NexusFSFileReader

        _reader = _NexusFSFileReader(_nx)
        _bm25 = _DaemonSkeletonBM25(_daemon)

        _session_factory = None
        _rs = svc.record_store
        if _rs is not None:
            with contextlib.suppress(AttributeError):
                _session_factory = _rs.async_session_factory

        _indexer = SkeletonIndexer(
            file_reader=_reader,
            bm25=_bm25,
            extractor_registry=SKELETON_EXTRACTOR_REGISTRY,
            async_session_factory=_session_factory,
        )
        app.state.skeleton_indexer = _indexer

        # Capture running loop now (startup_search runs in the event loop).
        # Passed to the consumer so _buffer() can use call_soon_threadsafe when
        # VFS hooks fire from asyncio.to_thread (sync context).
        _loop = asyncio.get_running_loop()

        # Consumer is created here but NOT started — startup_services calls
        # _startup_pipe_consumers after startup_search completes, and the Nexus
        # kernel pipe registry isn't ready until that phase.  The consumer is
        # stored on app.state so _startup_pipe_consumers can bind_fs + start it.
        _consumer = SkeletonPipeConsumer(indexer=_indexer, fallback_loop=_loop)
        app.state.skeleton_pipe_consumer = _consumer
        logger.debug("[SKELETON] SkeletonIndexer + SkeletonPipeConsumer created (pending start)")

        if not hasattr(_nx, "register_intercept_write"):
            return  # NexusFS doesn't support VFS hooks in this mode

        _zone_id = svc.zone_id or ROOT_ZONE_ID

        class _SkeletonWriteHook:
            @property
            def name(self) -> str:
                return "skeleton_auto_index"

            def on_post_write(self, ctx: object) -> None:
                _consumer.notify_write(
                    getattr(ctx, "path", ""),
                    getattr(ctx, "path_id", None),
                    getattr(ctx, "zone_id", None) or _zone_id,
                )

        class _SkeletonDeleteHook:
            @property
            def name(self) -> str:
                return "skeleton_auto_delete"

            def on_post_delete(self, ctx: object) -> None:
                _consumer.notify_delete(
                    getattr(ctx, "path", ""),
                    getattr(ctx, "path_id", None),
                    getattr(ctx, "zone_id", None) or _zone_id,
                )

        class _SkeletonRenameHook:
            @property
            def name(self) -> str:
                return "skeleton_auto_rename"

            def on_post_rename(self, ctx: object) -> None:
                _consumer.notify_rename(
                    getattr(ctx, "old_path", ""),
                    getattr(ctx, "new_path", ""),
                    getattr(ctx, "path_id", None),
                    getattr(ctx, "zone_id", None) or _zone_id,
                )

        _nx.register_intercept_write(_SkeletonWriteHook())
        _nx.register_intercept_delete(_SkeletonDeleteHook())
        _nx.register_intercept_rename(_SkeletonRenameHook())
        logger.info("[SKELETON] VFS auto-index hooks registered (write/delete/rename)")

    except Exception as e:
        logger.warning("[SKELETON] Skeleton indexer wiring failed (non-fatal): %s", e)
