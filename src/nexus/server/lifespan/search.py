"""Search startup: txtai-backed Search Daemon (Issue #2663).

Extracted from fastapi_server.py (#1602).
Rewritten for txtai backend (#2663).
"""

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_search(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Initialize search daemon and return background tasks."""
    search_daemon_enabled = os.getenv("NEXUS_SEARCH_DAEMON", "").lower() in (
        "true",
        "1",
        "yes",
    ) or (
        # Auto-enable if not explicitly disabled and database URL is set
        os.getenv("NEXUS_SEARCH_DAEMON", "").lower() not in ("false", "0", "no")
        and svc.database_url
    )

    if not search_daemon_enabled:
        logger.debug("Search Daemon disabled (set NEXUS_SEARCH_DAEMON=true to enable)")
        return []

    try:
        from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

        config = DaemonConfig(
            database_url=svc.database_url,
            query_timeout_seconds=float(os.environ.get("NEXUS_QUERY_TIMEOUT", "10.0")),
            # txtai backend config (Issue #2663)
            txtai_model=os.environ.get(
                "NEXUS_TXTAI_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
            ),
            txtai_reranker=os.environ.get("NEXUS_TXTAI_RERANKER") or None,
            txtai_sparse=os.environ.get("NEXUS_TXTAI_SPARSE", "").lower() in ("true", "1", "yes"),
            txtai_graph=os.environ.get("NEXUS_TXTAI_GRAPH", "true").lower()
            not in ("false", "0", "no"),
        )

        # Inject async_session_factory from RecordStoreABC when available
        _record_store = svc.record_store
        _async_sf = None
        if _record_store is not None:
            with contextlib.suppress(AttributeError):
                _async_sf = _record_store.async_session_factory

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

        app.state.search_daemon = SearchDaemon(
            config,
            async_session_factory=_async_sf,
            zoekt_client=_zoekt_client,
            cache_brick=_cache_brick,
        )

        # Embeddings are now handled by txtai backend (Issue #2663).
        # The old nexus.bricks.search.embeddings module has been deleted.

        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("search_daemon", app.state.search_daemon)
        else:
            await app.state.search_daemon.startup()
        app.state.search_daemon_enabled = True

        # Issue #1520: Set FileReaderProtocol for index refresh
        with contextlib.suppress(ImportError, AttributeError):
            from nexus.factory import _NexusFSFileReader

            app.state.search_daemon._file_reader = _NexusFSFileReader(svc.nexus_fs)

        # Wire SearchDaemon into SearchService so semantic_search queries
        # use the txtai backend instead of falling back to SQL ILIKE.
        with contextlib.suppress(AttributeError):
            search_svc = svc.nexus_fs.service("search")
            if search_svc is not None:
                search_svc._search_daemon = app.state.search_daemon

        # Auto-index on write/delete/rename: register VFS hooks that notify
        # the search daemon so the index stays fresh automatically.
        with contextlib.suppress(AttributeError, ImportError):
            _daemon_ref = app.state.search_daemon
            _dispatch = getattr(svc.nexus_fs, "_dispatch", None)
            if _dispatch is not None:
                import asyncio as _asyncio

                from nexus.contracts.vfs_hooks import (
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

                _dispatch.register_intercept_write(_SearchWriteHook())
                _dispatch.register_intercept_delete(_SearchDeleteHook())
                _dispatch.register_intercept_rename(_SearchRenameHook())
                logger.info("Search auto-index hooks registered (write/delete/rename)")

        # Issue #2036: Register with BrickLifecycleManager
        _blm = svc.brick_lifecycle_manager
        if _blm is not None:
            try:
                from nexus.bricks.search.lifecycle_adapter import (
                    SearchBrickLifecycleAdapter,
                )

                _blm.register(
                    "search",
                    SearchBrickLifecycleAdapter(app.state.search_daemon),
                    protocol_name="SearchBrickProtocol",
                )
            except ImportError:
                logger.debug("SearchBrickLifecycleAdapter not available, skipping registration")
            except Exception:
                logger.warning(
                    "Failed to register search brick with lifecycle manager", exc_info=True
                )

        stats = app.state.search_daemon.get_stats()
        logger.info(
            "Search Daemon started: backend=%s, startup=%.1fms",
            stats.get("backend", "txtai"),
            stats["startup_time_ms"],
        )

        # Issue #3147: Initialize ZoneSearchRegistry for federated search.
        # Phase 1: All zones use the single global daemon.
        # Phase 2: Per-zone daemons can be registered if ZoneManager is available.
        _init_zone_registry(app, svc)

    except Exception as e:
        logger.warning("Failed to start Search Daemon: %s", e)
        app.state.search_daemon_enabled = False

    return []


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
            for zone_id in zone_ids:
                caps = ZoneSearchCapabilities.from_daemon_stats(zone_id, daemon)
                registry.register(zone_id, daemon, capabilities=caps)
                # Phase 2: Push real capabilities to the Rust gRPC server so
                # remote nodes get accurate data from GetSearchCapabilities RPC.
                _py_mgr = getattr(zone_manager, "_py_mgr", None)
                if _py_mgr is not None and hasattr(_py_mgr, "set_search_capabilities"):
                    _py_mgr.set_search_capabilities(
                        zone_id,
                        caps.device_tier,
                        list(caps.search_modes),
                        caps.has_graph,
                        caps.embedding_model or "",
                        caps.embedding_dimensions,
                    )
            logger.info(
                "[ZONE-REGISTRY] Registered %d zones from ZoneManager",
                len(zone_ids),
            )
        except Exception as e:
            logger.warning("[ZONE-REGISTRY] Failed to register zones: %s", e)

    app.state.zone_search_registry = registry
