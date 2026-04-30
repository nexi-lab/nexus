"""SQLAlchemy ORM models mirroring the alembic schema (Issue #3790)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus.lib.db_base import Base

# Postgres production uses JSONB (indexable, typed). SQLite test fixtures that
# do `Base.metadata.create_all(...)` (e.g. test_seed_root_zone.py) must be
# able to compile the schema even though they never query approvals tables —
# `with_variant` degrades the column to plain JSON on SQLite without
# affecting the Postgres DDL or runtime ops.
_JSONB_PORTABLE = JSONB().with_variant(JSON(), "sqlite")


class ApprovalRequestModel(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Python attr suffix avoids DeclarativeBase.metadata collision;
    # DB column name stays "metadata".
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _JSONB_PORTABLE, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_approval_requests_status_expires", "status", "expires_at"),
        Index("ix_approval_requests_zone_status", "zone_id", "status"),
        # Load-bearing for request coalescing — only one pending row per
        # (zone_id, kind, subject). Both dialect predicates are required:
        # postgresql_where for production, sqlite_where for SQLite test
        # fixtures via create_all() / migration harness (#3790 round-13).
        Index(
            "approval_requests_pending_coalesce",
            "zone_id",
            "kind",
            "subject",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )


class ApprovalDecisionModel(Base):
    __tablename__ = "approval_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("approval_requests.id", ondelete="RESTRICT"), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_approval_decisions_request", "request_id"),)


class ApprovalSessionAllowModel(Base):
    __tablename__ = "approval_session_allow"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(512), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("approval_requests.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "zone_id", "kind", "subject", name="uq_approval_session_allow"
        ),
        Index("ix_approval_session_allow_session", "session_id"),
    )
