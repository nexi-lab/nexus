"""Shared base, mixins, and utilities for SQLAlchemy models.

Issue #1246 Phase 4: Extracted from monolithic models.py.
Issue #1286: Added mixins (TimestampMixin, ZoneIsolationMixin, ResourceConfigMixin),
             uuid_pk() helper, and lru_cache for _get_uuid_server_default.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import DateTime, String, Text, TextClause, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _generate_uuid() -> str:
    """Generate a UUID string.

    Returns UUIDv4 for maximum compatibility.
    PostgreSQL 18+ will use native uuidv7() via server_default for better index performance.
    """
    return str(uuid.uuid4())


@lru_cache(maxsize=1)
def _get_uuid_server_default() -> TextClause | None:
    """Get PostgreSQL server_default for UUID generation.

    Returns uuidv7()::text for PostgreSQL 18+ (timestamp-ordered UUIDs for better B-tree locality).
    Falls back to gen_random_uuid()::text for PostgreSQL < 18.
    Returns None for SQLite (uses Python default).

    Result is cached with lru_cache(maxsize=1) since the DB dialect doesn't change at runtime.
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


# ---------------------------------------------------------------------------
# Shared Mixins (Issue #1286)
# ---------------------------------------------------------------------------


class TimestampMixin:
    """Mixin providing created_at and updated_at timestamp columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ZoneIsolationMixin:
    """Mixin providing a zone_id column for multi-zone isolation."""

    zone_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )


class ResourceConfigMixin:
    """Mixin for shared fields between WorkspaceConfigModel and MemoryConfigModel."""

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="persistent"
    )
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extra_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)


def uuid_pk() -> Mapped[str]:
    """Create a standard UUID primary key column.

    Returns a mapped_column configured with UUID generation for both
    Python default and PostgreSQL server_default.

    Usage:
        class MyModel(Base):
            id: Mapped[str] = uuid_pk()
    """
    return mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
