"""Migration: add context_manifest column to agent_records table (Issue #1427).

Adds the ``context_manifest`` TEXT column to ``agent_records`` for storing
serialized context source definitions. Safe to run multiple times (idempotent).

The column is nullable with a default of '[]' (empty JSON array).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

COLUMN_NAME = "context_manifest"
TABLE_NAME = "agent_records"

# Safety: validate identifiers against an allowlist pattern to prevent
# any future accidental SQL injection if constants are ever parameterized.
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against a safe pattern."""
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def needs_migration(session: Session) -> bool:
    """Check if the context_manifest column already exists.

    Args:
        session: SQLAlchemy database session (must have a bind).

    Returns:
        True if migration is needed (column missing), False if already present.
    """
    if session.bind is None:
        raise ValueError("Session must have a bind")
    inspector = inspect(session.bind)

    if TABLE_NAME not in inspector.get_table_names():
        # Table doesn't exist yet — create_all() will handle it
        return False

    columns = {col["name"] for col in inspector.get_columns(TABLE_NAME)}
    return COLUMN_NAME not in columns


def run_migration(session: Session) -> bool:
    """Add context_manifest column to agent_records if missing.

    Idempotent: returns False if column already exists.

    Args:
        session: SQLAlchemy database session.

    Returns:
        True if migration was applied, False if not needed.
    """
    if not needs_migration(session):
        logger.debug("[MIGRATION] context_manifest column already exists, skipping")
        return False

    table = _validate_identifier(TABLE_NAME)
    column = _validate_identifier(COLUMN_NAME)

    logger.info("[MIGRATION] Adding %s column to %s", column, table)
    session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT DEFAULT '[]'"))
    session.commit()
    logger.info("[MIGRATION] context_manifest column added successfully")
    return True
