"""Search startup: Hot Search Daemon (Issue #951).

Extracted from fastapi_server.py (#1602).
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

        # Issue #2071: source max_indexing_concurrency from profile tuning
        _search_tuning = svc.profile_tuning
        _max_indexing = _search_tuning.search.search_max_concurrency if _search_tuning else None

        _daemon_kwargs: dict = {
            "database_url": svc.database_url,
            "bm25s_index_dir": os.getenv("NEXUS_BM25S_INDEX_DIR", ".nexus-data/bm25s"),
            "db_pool_min_size": int(os.getenv("NEXUS_SEARCH_POOL_MIN", "10")),
            "db_pool_max_size": int(os.getenv("NEXUS_SEARCH_POOL_MAX", "50")),
            "refresh_enabled": os.getenv("NEXUS_SEARCH_REFRESH", "true").lower()
            in ("true", "1", "yes"),
            # Issue #1024: Entropy-aware filtering for redundant content
            "entropy_filtering": os.getenv("NEXUS_ENTROPY_FILTERING", "false").lower()
            in ("true", "1", "yes"),
            "entropy_threshold": float(os.getenv("NEXUS_ENTROPY_THRESHOLD", "0.35")),
            "entropy_alpha": float(os.getenv("NEXUS_ENTROPY_ALPHA", "0.5")),
        }
        if _max_indexing is not None:
            _daemon_kwargs["max_indexing_concurrency"] = _max_indexing

        config = DaemonConfig(**_daemon_kwargs)

        # Inject async_session_factory from RecordStoreABC when available
        _record_store = svc.record_store
        _async_sf = None
        if _record_store is not None:
            with contextlib.suppress(Exception):
                _async_sf = _record_store.async_session_factory

        # Issue #2188: Create ZoektClient via DI (no module-level globals)
        _zoekt_client = None
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

        app.state.search_daemon = SearchDaemon(
            config,
            async_session_factory=_async_sf,
            zoekt_client=_zoekt_client,
        )
        await app.state.search_daemon.startup()
        app.state.search_daemon_enabled = True

        # Issue #1520: Set FileReaderProtocol for index refresh (replaces _nexus_fs)
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
            "Search Daemon started: %d docs indexed, startup=%.1fms",
            stats["bm25_documents"],
            stats["startup_time_ms"],
        )
    except Exception as e:
        logger.warning("Failed to start Search Daemon: %s", e)
        app.state.search_daemon_enabled = False

    return []
