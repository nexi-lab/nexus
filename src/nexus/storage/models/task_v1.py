"""P1b implicit Task/Resolution/Attempt persistence.

These rows are the authoritative database records.  The matching JSON files
written under the Session home Zone are immutable creation snapshots used as
physical placement evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskSpecModel(Base):
    """One write-once implicit TaskSpec per Session."""

    __tablename__ = "task_specs"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_task_specs_session"),
        Index("ix_task_specs_session", "session_id"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[dict] = mapped_column(GRANTEE_JSON, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    resource_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    requested_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vfs_path: Mapped[str] = mapped_column(String(512), nullable=False)
    bytes_written: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class TaskResolutionModel(Base):
    """Append-only policy decision for an implicit Task."""

    __tablename__ = "task_resolutions"
    __table_args__ = (
        CheckConstraint("status IN ('accepted', 'rejected')", name="ck_task_resolution_status"),
        CheckConstraint(
            "status = 'rejected' OR (attempt_id IS NOT NULL AND execution_zone_id IS NOT NULL)",
            name="ck_task_resolution_accepted_refs",
        ),
        Index("ix_task_resolutions_task", "task_id", "decided_at"),
    )

    resolution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_specs.task_id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vfs_path: Mapped[str] = mapped_column(String(512), nullable=False)
    bytes_written: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class TaskAttemptModel(Base):
    """True policy Attempt, distinct from a RuntimeRun/PID generation."""

    __tablename__ = "task_attempts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'starting', 'running', 'awaiting_input', "
            "'output_ready', 'verifying', 'completed', 'failed', 'cancelled')",
            name="ck_task_attempt_state",
        ),
        Index("ix_task_attempts_task", "task_id", "created_at"),
        Index("ix_task_attempts_session", "session_id", "created_at"),
    )

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_specs.task_id", ondelete="RESTRICT"), nullable=False
    )
    resolution_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("task_resolutions.resolution_id", ondelete="RESTRICT"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=False
    )
    execution_zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    pid_history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    failure: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vfs_path: Mapped[str] = mapped_column(String(512), nullable=False)
    bytes_written: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
