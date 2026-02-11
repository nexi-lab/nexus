"""Shared base and utilities for SQLAlchemy models.

Issue #1246 Phase 4: Extracted from monolithic models.py.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import TextClause, text
from sqlalchemy.orm import DeclarativeBase


def _generate_uuid() -> str:
    """Generate a UUID string.

    Returns UUIDv4 for maximum compatibility.
    PostgreSQL 18+ will use native uuidv7() via server_default for better index performance.
    """
    return str(uuid.uuid4())


def _get_uuid_server_default() -> TextClause | None:
    """Get PostgreSQL server_default for UUID generation.

    Returns uuidv7()::text for PostgreSQL 18+ (timestamp-ordered UUIDs for better B-tree locality).
    Falls back to gen_random_uuid()::text for PostgreSQL < 18.
    Returns None for SQLite (uses Python default).
    """
    db_url = os.environ.get("NEXUS_DATABASE_URL", "")
    if not db_url.startswith(("postgres", "postgresql")):
        return None

    try:
        from sqlalchemy import create_engine
        from sqlalchemy import text as sa_text

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT uuidv7()"))
        engine.dispose()
        return text("uuidv7()::text")
    except Exception:
        return text("gen_random_uuid()::text")


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass
