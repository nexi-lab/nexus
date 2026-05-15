"""ReBAC zone finalizer — bulk-deletes zone-scoped authorization tuples (Issue #2061).

MUST run last (Decision #13A) — other finalizers may still need
ReBAC data for permission checks during their cleanup.
"""

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from nexus.contracts.protocols.zone_lifecycle import REBAC_FINALIZER_KEY

logger = logging.getLogger(__name__)


class ReBACZoneFinalizer:
    """Finalizer that removes all ReBAC tuples for a zone (runs last)."""

    def __init__(self, session_factory: Callable[..., Any]) -> None:
        self._session_factory = session_factory

    @property
    def finalizer_key(self) -> str:
        return REBAC_FINALIZER_KEY

    async def finalize_zone(self, zone_id: str) -> None:
        """Bulk-delete all ReBAC tuples for *zone_id*."""
        with self._session_factory() as session:
            result = session.execute(
                text("DELETE FROM rebac_tuples WHERE zone_id = :zid"),
                {"zid": zone_id},
            )
            deleted = result.rowcount
            session.commit()

        logger.info(
            "[ReBACFinalizer] Deleted %d tuples for zone %s",
            deleted,
            zone_id,
        )
