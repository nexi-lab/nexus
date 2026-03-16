"""Shared ORM base, mixins, and utilities for SQLAlchemy models.

Canonical location for ``Base``, ``TimestampMixin``, ``ZoneIsolationMixin``,
``ResourceConfigMixin``, and ``uuid_pk``.  Lives in ``nexus.lib`` (tier-neutral
implementation helpers) so that both kernel code and bricks can depend on it
without pulling in storage internals.

History:
    nexus.storage.models._base → nexus.contracts.db_base → nexus.lib.db_base
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, TextClause, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID


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
    from nexus.lib.env import get_database_url

    db_url = get_database_url() or ""
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

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)


class ResourceConfigMixin:
    """Mixin for resource configuration fields (identity, scope, TTL)."""

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
