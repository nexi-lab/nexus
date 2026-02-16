"""Revision helpers — Version tokens and zone revision lookups.

Extracts the consistency-related methods from EnhancedReBACManager
into standalone functions. These support Zanzibar-style consistency
tokens (zookies) for snapshot reads and bounded staleness.

Usage:
    from nexus.rebac.consistency.revision import (
        increment_version_token,
        get_zone_revision_for_grant,
    )

    token = increment_version_token(engine, conn_helper, zone_id="org_acme")
    revision = get_zone_revision_for_grant(engine, zone_id="org_acme")

Related: Issue #1459 (decomposition), P0-1 (consistency levels)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.exc import OperationalError, ProgrammingError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


class ConnectionHelper(Protocol):
    """Protocol for database connection helpers.

    Matches the interface provided by ReBACManager / TupleRepository.
    """

    def connection(self) -> Any:
        """Context manager yielding a DBAPI connection."""
        ...

    def create_cursor(self, conn: Any) -> Any:
        """Create a cursor with appropriate factory."""
        ...

    def fix_sql_placeholders(self, sql: str) -> str:
        """Convert ? placeholders to %s for PostgreSQL."""
        ...


def increment_version_token(
    engine: Engine,
    conn_helper: ConnectionHelper,
    zone_id: str = "default",
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
        conn_helper: Connection helper (for DB access)
        zone_id: Zone ID to increment version for

    Returns:
        Monotonic version token string (e.g., "v123")
    """
    with conn_helper.connection() as conn:
        cursor = conn_helper.create_cursor(conn)

        if engine.dialect.name == "postgresql":
            # Atomic increment-and-return
            cursor.execute(
                """
                INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                VALUES (%s, 1, NOW())
                ON CONFLICT (zone_id)
                DO UPDATE SET current_version = rebac_version_sequences.current_version + 1,
                              updated_at = NOW()
                RETURNING current_version
                """,
                (zone_id,),
            )
            row = cursor.fetchone()
            version = row["current_version"] if row else 1
        else:
            # SQLite: Atomic INSERT OR IGNORE + UPDATE to avoid race conditions
            now_iso = datetime.now(UTC).isoformat()
            cursor.execute(
                conn_helper.fix_sql_placeholders(
                    """
                    INSERT OR IGNORE INTO rebac_version_sequences
                        (zone_id, current_version, updated_at)
                    VALUES (?, 0, ?)
                    """
                ),
                (zone_id, now_iso),
            )
            cursor.execute(
                conn_helper.fix_sql_placeholders(
                    """
                    UPDATE rebac_version_sequences
                    SET current_version = current_version + 1, updated_at = ?
                    WHERE zone_id = ?
                    """
                ),
                (now_iso, zone_id),
            )
            cursor.execute(
                conn_helper.fix_sql_placeholders(
                    "SELECT current_version FROM rebac_version_sequences WHERE zone_id = ?"
                ),
                (zone_id,),
            )
            row = cursor.fetchone()
            version = row["current_version"] if row else 1

        conn.commit()
        return f"v{version}"


def get_zone_revision_for_grant(engine: Engine, zone_id: str) -> int:
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
    from sqlalchemy import text

    try:
        query = text("""
            SELECT current_version FROM rebac_version_sequences
            WHERE zone_id = :zone_id
        """)
        with engine.connect() as conn:
            result = conn.execute(query, {"zone_id": zone_id})
            row = result.fetchone()
            return int(row.current_version) if row else 0
    except (OperationalError, ProgrammingError):
        return 0
