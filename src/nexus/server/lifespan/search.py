"""Search startup: Hot Search Daemon (Issue #951).

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_search(app: FastAPI) -> list[asyncio.Task]:
    """Initialize search daemon and return background tasks."""
    search_daemon_enabled = os.getenv("NEXUS_SEARCH_DAEMON", "").lower() in (
        "true",
        "1",
        "yes",
    ) or (
        # Auto-enable if not explicitly disabled and database URL is set
        os.getenv("NEXUS_SEARCH_DAEMON", "").lower() not in ("false", "0", "no")
        and app.state.database_url
    )

    if not search_daemon_enabled:
        logger.debug("Search Daemon disabled (set NEXUS_SEARCH_DAEMON=true to enable)")
        return []

    try:
        from nexus.search.daemon import DaemonConfig, SearchDaemon, set_search_daemon

        config = DaemonConfig(
            database_url=app.state.database_url,
            bm25s_index_dir=os.getenv("NEXUS_BM25S_INDEX_DIR", ".nexus-data/bm25s"),
            db_pool_min_size=int(os.getenv("NEXUS_SEARCH_POOL_MIN", "10")),
            db_pool_max_size=int(os.getenv("NEXUS_SEARCH_POOL_MAX", "50")),
            refresh_enabled=os.getenv("NEXUS_SEARCH_REFRESH", "true").lower()
            in (
                "true",
                "1",
                "yes",
            ),
            # Issue #1024: Entropy-aware filtering for redundant content
            entropy_filtering=os.getenv("NEXUS_ENTROPY_FILTERING", "false").lower()
            in ("true", "1", "yes"),
            entropy_threshold=float(os.getenv("NEXUS_ENTROPY_THRESHOLD", "0.35")),
            entropy_alpha=float(os.getenv("NEXUS_ENTROPY_ALPHA", "0.5")),
        )

        app.state.search_daemon = SearchDaemon(config)
        await app.state.search_daemon.startup()
        app.state.search_daemon_enabled = True
        set_search_daemon(app.state.search_daemon)

        # Set NexusFS reference for index refresh (Issue #1024)
        app.state.search_daemon._nexus_fs = app.state.nexus_fs

        stats = app.state.search_daemon.get_stats()
        logger.info(
            f"Search Daemon started: {stats['bm25_documents']} docs indexed, "
            f"startup={stats['startup_time_ms']:.1f}ms"
        )
    except Exception as e:
        logger.warning(f"Failed to start Search Daemon: {e}")
        app.state.search_daemon_enabled = False

    return []
