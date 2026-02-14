"""API v1 router registry and registration (#1288).

Mirrors the v2 versioning pattern:
- build_v1_registry(): Import all v1 routers, return a populated RouterRegistry.
- register_v1_routers(): One-call registration on a FastAPI app.

Issue #1288: Decompose FastAPI server monolith into domain routers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.server.api.v2.versioning import RouterEntry, RouterRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def build_v1_registry() -> RouterRegistry:
    """Import all v1 routers and return a populated registry.

    Each import is isolated in its own try/except so one broken
    module doesn't prevent the rest from loading.
    """
    registry = RouterRegistry()

    # ---- Locks router (Issue #1186) ----
    try:
        from nexus.server.api.v1.routers.locks import router as locks_router

        registry.add(RouterEntry(router=locks_router, name="locks", endpoint_count=5))
    except ImportError as e:
        logger.warning("Failed to import locks router: %s", e)

    # ---- Subscriptions router ----
    try:
        from nexus.server.api.v1.routers.subscriptions import router as subscriptions_router

        registry.add(
            RouterEntry(router=subscriptions_router, name="subscriptions", endpoint_count=6)
        )
    except ImportError as e:
        logger.warning("Failed to import subscriptions router: %s", e)

    # ---- Identity router (Issue #1355) ----
    try:
        from nexus.server.api.v1.routers.identity import router as identity_router

        registry.add(RouterEntry(router=identity_router, name="identity", endpoint_count=2))
    except ImportError as e:
        logger.warning("Failed to import identity router: %s", e)

    # ---- Search router (Issue #951) ----
    try:
        from nexus.server.api.v1.routers.search import router as search_router

        registry.add(RouterEntry(router=search_router, name="search", endpoint_count=5))
    except ImportError as e:
        logger.warning("Failed to import search router: %s", e)

    # ---- Memory router (Issue #1023) ----
    try:
        from nexus.server.api.v1.routers.memory import router as memory_router

        registry.add(RouterEntry(router=memory_router, name="memory", endpoint_count=4))
    except ImportError as e:
        logger.warning("Failed to import memory router: %s", e)

    # ---- Graph router (Issue #1039) ----
    try:
        from nexus.server.api.v1.routers.graph import router as graph_router

        registry.add(RouterEntry(router=graph_router, name="graph", endpoint_count=4))
    except ImportError as e:
        logger.warning("Failed to import graph router: %s", e)

    # ---- Admin router (Issue #921) ----
    try:
        from nexus.server.api.v1.routers.admin import router as admin_router

        registry.add(RouterEntry(router=admin_router, name="admin", endpoint_count=2))
    except ImportError as e:
        logger.warning("Failed to import admin router: %s", e)

    # ---- Cache router (Issue #1076) ----
    try:
        from nexus.server.api.v1.routers.cache import router as cache_router

        registry.add(RouterEntry(router=cache_router, name="cache", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import cache router: %s", e)

    # ---- Share router (Issue #227) ----
    try:
        from nexus.server.api.v1.routers.share import router as share_router

        registry.add(RouterEntry(router=share_router, name="share", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import share router: %s", e)

    # ---- Events router (Issue #1116, #1117) ----
    try:
        from nexus.server.api.v1.routers.events import router as events_router

        registry.add(RouterEntry(router=events_router, name="events", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import events router: %s", e)

    return registry


def register_v1_routers(app: FastAPI, registry: RouterRegistry) -> None:
    """Mount every router in *registry* onto *app*."""
    for entry in registry.entries:
        if entry.prefix is not None:
            app.include_router(entry.router, prefix=entry.prefix)
        else:
            app.include_router(entry.router)

    total = registry.total_endpoints()
    logger.info("API v1 routers registered (%d endpoints)", total)
