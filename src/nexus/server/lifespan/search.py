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
        )

        # Inject async_session_factory from RecordStoreABC when available
        _record_store = svc.record_store
        _async_sf = None
        if _record_store is not None:
            with contextlib.suppress(Exception):
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

        # Wire embedding provider so semantic search works (pre-existing gap:
        # _embedding_provider was always None, causing silent fallback failures)
        if _search_cfg is not None:
            with contextlib.suppress(Exception):
                from nexus.bricks.search.embeddings import create_embedding_provider

                app.state.search_daemon._embedding_provider = create_embedding_provider(
                    provider=_search_cfg.embedding_provider,
                    model=_search_cfg.embedding_model,
                )
                logger.info(
                    "Embedding provider wired: %s/%s",
                    _search_cfg.embedding_provider,
                    _search_cfg.embedding_model or "default",
                )

        await app.state.search_daemon.startup()
        app.state.search_daemon_enabled = True

        # Issue #1520: Set FileReaderProtocol for index refresh
        with contextlib.suppress(Exception):
            from nexus.factory import _NexusFSFileReader

            app.state.search_daemon._file_reader = _NexusFSFileReader(svc.nexus_fs)

        # Issue #2036: Inject AdaptiveKProtocol (LEGO compliance)
        with contextlib.suppress(Exception):
            from nexus.bricks.llm.llm_context_builder import ContextBuilder

            app.state.search_daemon._adaptive_k_provider = ContextBuilder()

        # Issue #2036: Register with BrickLifecycleManager
        _blm = svc.brick_lifecycle_manager
        if _blm is not None:
            with contextlib.suppress(Exception):
                from nexus.bricks.search.lifecycle_adapter import (
                    SearchBrickLifecycleAdapter,
                )

                _blm.register(
                    "search",
                    SearchBrickLifecycleAdapter(app.state.search_daemon),
                    protocol_name="SearchBrickProtocol",
                )

        stats = app.state.search_daemon.get_stats()
        logger.info(
            "Search Daemon started: backend=%s, startup=%.1fms",
            stats.get("backend", "txtai"),
            stats["startup_time_ms"],
        )
    except Exception as e:
        logger.warning("Failed to start Search Daemon: %s", e)
        app.state.search_daemon_enabled = False

    return []
