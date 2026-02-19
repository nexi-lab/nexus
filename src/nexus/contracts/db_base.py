"""Shared ORM base, mixins, and utilities for SQLAlchemy models.

Canonical location for ``Base``, ``TimestampMixin``, ``ZoneIsolationMixin``,
``ResourceConfigMixin``, and ``uuid_pk``.  This module lives in
``nexus.contracts`` (tier-neutral) so that both kernel code and bricks can
depend on it without pulling in storage internals.

History:
    Originally in ``nexus.storage.models._base`` (Issue #1246 / #1286).
    Moved here by Issue #2129 (governance brick extraction) to break the
    storage ↔ brick import cycle.

Backward compatibility:
    ``from nexus.storage.models._base import Base`` still works via re-exports
    in ``nexus/storage/models/_base.py``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, TextClause, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _generate_uuid() -> str:
    """Generate a UUID string (UUIDv4).

    Used as Python-side default for UUID primary keys.
    PostgreSQL uses gen_random_uuid() via server_default.
    """
    return str(uuid.uuid4())


def _get_uuid_server_default() -> TextClause | None:
    """Get PostgreSQL server_default for UUID generation.

    Returns gen_random_uuid()::text for PostgreSQL (available on PG 13+).
    Returns None for SQLite (uses Python default via _generate_uuid).

    Only checks the database URL string — no engine creation or DB probing at import time.
    """
    db_url = os.environ.get("NEXUS_DATABASE_URL", "")
    if db_url.startswith(("postgres", "postgresql")):
        return text("gen_random_uuid()::text")
    return None


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

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="root")


class ResourceConfigMixin:
    """Mixin for shared fields between WorkspaceConfigModel and MemoryConfigModel."""

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="persistent")
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
