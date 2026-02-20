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

    All v1 routers have been ported to v2 (#2056). This registry
    is now empty and retained only for the app factory interface.
    """
    return RouterRegistry()


def register_v1_routers(app: FastAPI, registry: RouterRegistry) -> None:
    """Mount every router in *registry* onto *app*."""
    for entry in registry.entries:
        if entry.prefix is not None:
            app.include_router(entry.router, prefix=entry.prefix)
        else:
            app.include_router(entry.router)

    total = registry.total_endpoints()
    logger.info("API v1 routers registered (%d endpoints)", total)
