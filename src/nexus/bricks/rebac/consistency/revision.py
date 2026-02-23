"""Revision helpers — Version tokens and zone revision lookups.

Extracts the consistency-related methods from ReBACManager
into standalone functions. These support Zanzibar-style consistency
tokens (zookies) for snapshot reads and bounded staleness.

Usage:
    from nexus.bricks.rebac.consistency.revision import (
        increment_version_token,
        get_zone_revision_for_grant,
    )

    token = increment_version_token(engine, zone_id="org_acme")
    revision = get_zone_revision_for_grant(engine, zone_id="org_acme")

Related: Issue #1459 (decomposition), P0-1 (consistency levels)
"""

import logging
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError, ProgrammingError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models.permissions import ReBACVersionSequenceModel as RBVS

logger = logging.getLogger(__name__)


def increment_version_token(
    engine: "Engine",
    zone_id: str = ROOT_ZONE_ID,
    *,
    is_postgresql: bool = False,
) -> str:
    """Atomically increment and return the version token for a zone.

    Uses atomic INSERT ... ON CONFLICT DO UPDATE for PostgreSQL
    and a two-step SELECT + UPDATE for SQLite. Each call increments
    the zone's version counter and returns the new value.

    BUGFIX (Issue #2): Uses DB-backed per-zone sequence instead of
    in-memory counter. This ensures version tokens are:
    - Monotonic across process restarts
    - Consistent across multiple processes/replicas
    - Scoped per-zone for proper isolation

    Args:
        engine: SQLAlchemy engine (for dialect detection)
        zone_id: Zone ID to increment version for

    Returns:
        Monotonic version token string (e.g., "v123")
    """
    if is_postgresql:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(RBVS)
            .values(zone_id=zone_id, current_version=1, updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[RBVS.zone_id],
                set_={
                    "current_version": RBVS.current_version + 1,
                    "updated_at": func.now(),
                },
            )
            .returning(RBVS.current_version)
        )
        with engine.begin() as conn:
            result = conn.execute(stmt)
            row = result.fetchone()
            version = row.current_version if row else 1
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        # SQLite: INSERT OR IGNORE + UPDATE to avoid race conditions
        insert_stmt = (
            sqlite_insert(RBVS)
            .values(zone_id=zone_id, current_version=0, updated_at=func.now())
            .on_conflict_do_nothing(index_elements=[RBVS.zone_id])
        )
        update_stmt = (
            update(RBVS)
            .where(RBVS.zone_id == zone_id)
            .values(
                current_version=RBVS.current_version + 1,
                updated_at=func.now(),
            )
        )
        select_stmt = select(RBVS.current_version).where(RBVS.zone_id == zone_id)

        with engine.begin() as conn:
            conn.execute(insert_stmt)
            conn.execute(update_stmt)
            row = conn.execute(select_stmt).fetchone()
            version = row.current_version if row else 1

    return f"v{version}"


def get_zone_revision_for_grant(engine: "Engine", zone_id: str) -> int:
    """Get current zone revision for consistency during expansion.

    This prevents the "new enemy" problem: files created after the grant
    revision are not automatically included (user must explicitly include
    future files or re-grant).

    Args:
        engine: SQLAlchemy engine
        zone_id: Zone ID

    Returns:
        Current revision number (0 if not found or on error)
    """
    try:
        stmt = select(RBVS.current_version).where(RBVS.zone_id == zone_id)
        with engine.connect() as conn:
            row = conn.execute(stmt).fetchone()
            return int(row.current_version) if row else 0
    except (OperationalError, ProgrammingError):
        return 0
