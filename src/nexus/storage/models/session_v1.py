"""P1a session/runtime storage (SW-20260915-002 §8.9).

Canonical SessionStore for the P1a runtime-zone subset:

- ``sessions`` — session record with the immutable ``home_zone_id``
  (written once by the authenticated ingress; a second write is rejected,
  never overwritten);
- ``session_runtime_runs`` — PID/runtime descriptors solidifying
  ``execution_zone_id`` plus delegation/grant/epoch references
  (the recorded ADR-001 P1a amendment, not a freeze);
- ``session_zone_dependencies`` — grant/epoch → active runtime dependency
  index powering cancellation and revocation_pending;
- ``session_data_records`` — the home-zone routing ledger for Session
  metadata / Transcript / Context / Artifact / Verify writes: every record
  write stores the zone it landed in and the zone-scoped VFS path, giving
  restart-stable, auditable proof of the default routing.

The five record kinds default-write into the session's home zone through
the typed kernel (real I/O), not through SQL columns alone.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base
from .zone_v1 import GRANTEE_JSON

#: §8.9 item 2 — the record kinds the SessionStore routes into the home zone.
SESSION_RECORD_KINDS: tuple[str, ...] = (
    "session",  # session metadata itself
    "transcript",
    "context",
    "artifact",
    "verify",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SessionModel(Base):
    """Session record; home_zone_id is immutable after creation."""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_home_zone", "home_zone_id"),)

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    home_zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[dict] = mapped_column(GRANTEE_JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    agent_principal: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by: Mapped[dict] = mapped_column(GRANTEE_JSON, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="p1a-default")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SessionRuntimeRunModel(Base):
    """PID/runtime descriptor (ADR-001 §3.3 + P1a amendment fields)."""

    __tablename__ = "session_runtime_runs"
    __table_args__ = (
        Index("ix_runtime_runs_session", "session_id"),
        Index("ix_runtime_runs_zone", "execution_zone_id"),
    )

    pid: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=False
    )
    execution_zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delegation_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    grant_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SessionZoneDependencyModel(Base):
    """grant/epoch → active runtime dependency index (§8.9 item 4)."""

    __tablename__ = "session_zone_dependencies"
    __table_args__ = (
        UniqueConstraint("zone_id", "grant_ref", "pid", name="uq_session_dep_grant_pid"),
        Index("ix_session_dep_epoch", "zone_id", "authorization_epoch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pid: Mapped[str] = mapped_column(
        String(64), ForeignKey("session_runtime_runs.pid", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    grant_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SessionDataRecordModel(Base):
    """Home-zone routing ledger: what landed where, with real VFS I/O."""

    __tablename__ = "session_data_records"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "record_kind", "record_name", name="uq_session_record_named"
        ),
        Index("ix_session_records_zone", "zone_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=False
    )
    record_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    record_name: Mapped[str] = mapped_column(String(256), nullable=False, default="default")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vfs_path: Mapped[str] = mapped_column(String(512), nullable=False)
    bytes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # restart/resume does not drift: the resolved zone is recorded per write
    # and the ledger is append-mostly (updates only bump bytes/updated_at).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


_ = Boolean  # imported for future flags; keep linters quiet
