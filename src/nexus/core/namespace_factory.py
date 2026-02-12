"""Factory function for NamespaceManager creation (Issue #1265).

DRY: replaces duplicated NamespaceManager construction in fastapi_server.py.
Configures L3 persistent view store when an Engine is provided.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from nexus.core.namespace_manager import NamespaceManager

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


def create_namespace_manager(
    rebac_manager: EnhancedReBACManager,
    engine: Engine | None = None,
) -> NamespaceManager:
    """Create NamespaceManager with config from environment variables.

    Args:
        rebac_manager: EnhancedReBACManager for ReBAC queries
        engine: SQLAlchemy Engine for L3 persistent view store.
            If None, L3 is disabled (graceful degradation).

    Returns:
        Configured NamespaceManager instance.
    """
    cache_ttl = int(os.getenv("NEXUS_NAMESPACE_CACHE_TTL", "300"))
    revision_window = int(os.getenv("NEXUS_NAMESPACE_REVISION_WINDOW", "10"))

    persistent_store = None
    if engine is not None:
        try:
            from nexus.cache.persistent_view_postgres import PostgresPersistentViewStore

            persistent_store = PostgresPersistentViewStore(engine)
            logger.info("[NAMESPACE] L3 persistent view store enabled (PostgreSQL)")
        except Exception:
            logger.warning(
                "[NAMESPACE] Failed to initialize L3 persistent view store, "
                "falling back to L2-only mode",
                exc_info=True,
            )

    return NamespaceManager(
        rebac_manager=rebac_manager,
        cache_maxsize=10_000,
        cache_ttl=cache_ttl,
        revision_window=revision_window,
        persistent_store=persistent_store,
    )
