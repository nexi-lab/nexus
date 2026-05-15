from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def shutdown_zone_runners(app: "FastAPI", svc: "LifespanServices") -> None:
    registry = getattr(app.state, "zone_registry", None) or getattr(svc, "zone_registry", None)
    if registry is None:
        return
    try:
        await asyncio.to_thread(registry.stop_all)
        logger.info("Zone runners stopped")
    except Exception:
        logger.exception("Failed to stop zone runners")
