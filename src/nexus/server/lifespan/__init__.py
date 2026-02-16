"""Lifespan management for the FastAPI Nexus server.

Extracted from fastapi_server.py (#1602). The lifespan orchestrator calls
domain-specific initializers during startup and shuts them down in reverse
order during shutdown.

Each initializer function:
- Accepts ``app: FastAPI`` (reads/writes ``app.state``)
- Returns a list of ``asyncio.Task`` references that must be cancelled on shutdown
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager.

    Calls domain-specific initializers during startup and tears them
    down in reverse order during shutdown.
    """
    from nexus.server.lifespan.observability import (
        shutdown_observability,
        startup_observability,
    )
    from nexus.server.lifespan.permissions import startup_permissions
    from nexus.server.lifespan.realtime import shutdown_realtime, startup_realtime
    from nexus.server.lifespan.search import startup_search
    from nexus.server.lifespan.services import shutdown_services, startup_services
    from nexus.server.lifespan.uploads import startup_uploads

    # Collect all background tasks for clean shutdown
    bg_tasks: list[asyncio.Task] = []

    # --- Startup (order matters: observability first, then core, then services) ---

    startup_observability(app)
    bg_tasks.extend(await startup_permissions(app))
    bg_tasks.extend(await startup_realtime(app))
    bg_tasks.extend(await startup_search(app))
    bg_tasks.extend(await startup_services(app))
    bg_tasks.extend(await startup_uploads(app))

    yield

    # --- Shutdown (reverse order) ---
    logger.info("Shutting down FastAPI Nexus server...")

    # Cancel all background tasks first
    for task in bg_tasks:
        if task and not task.done():
            task.cancel()
    if bg_tasks:
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*[t for t in bg_tasks if t], return_exceptions=True)
        logger.debug(f"Cancelled {len(bg_tasks)} background tasks")

    await shutdown_services(app)
    await shutdown_realtime(app)

    # Close NexusFS kernel
    if app.state.nexus_fs:
        # Stop WriteBuffer to drain pending events before closing kernel (Issue #1370)
        _wo = getattr(app.state.nexus_fs, "_write_observer", None)
        if _wo is not None and hasattr(_wo, "stop"):
            try:
                _wo.stop()
                logger.info("WriteBuffer stopped")
            except Exception as e:
                logger.warning(f"Error stopping WriteBuffer: {e}")

        if hasattr(app.state.nexus_fs, "close"):
            app.state.nexus_fs.close()

    # Shutdown cache factory (Issue #1075)
    if hasattr(app.state, "cache_factory") and app.state.cache_factory:
        try:
            await app.state.cache_factory.shutdown()
            logger.info("Cache factory stopped")
        except Exception as e:
            logger.warning(f"Error shutting down cache factory: {e}")

    shutdown_observability()
