"""Search zone finalizer — bulk-deletes zone-scoped search index entries (Issue #2061).

Performs batch ``DELETE FROM entities WHERE zone_id = ?`` and
``DELETE FROM relationships WHERE zone_id = ?`` to remove orphaned
search data when a zone is deprovisioned.
"""

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)


class SearchZoneFinalizer:
    """Finalizer that removes all search-indexed entities and relationships for a zone."""

    def __init__(self, session_factory: Callable[..., Any]) -> None:
        self._session_factory = session_factory

    @property
    def finalizer_key(self) -> str:
        return "nexus.core/search"

    async def finalize_zone(self, zone_id: str) -> None:
        """Bulk-delete search index entries for *zone_id*.

        .. todo:: Issue #2070: consider batched DELETE with LIMIT for zones with
           millions of rows to avoid long-running transactions.
        """
        with self._session_factory() as session:
            # Delete entities
            result_entities = session.execute(
                text("DELETE FROM entities WHERE zone_id = :zid"),
                {"zid": zone_id},
            )
            entities_deleted = result_entities.rowcount

            # Delete relationships
            result_rels = session.execute(
                text("DELETE FROM relationships WHERE zone_id = :zid"),
                {"zid": zone_id},
            )
            rels_deleted = result_rels.rowcount

            session.commit()

        logger.info(
            "[SearchFinalizer] Deleted %d entities + %d relationships for zone %s",
            entities_deleted,
            rels_deleted,
            zone_id,
        )
