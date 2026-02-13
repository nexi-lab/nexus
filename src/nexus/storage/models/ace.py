"""ACE (Agentic Context Engineering) models — trajectories, feedback, playbooks.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.exceptions import ValidationError
from nexus.storage.models._base import Base, uuid_pk


class TrajectoryModel(Base):
    """Trajectory tracking for ACE (Agentic Context Engineering).

    Tracks execution trajectories for learning and reflection.
    Each trajectory represents a task execution with steps, decisions, and outcomes.
    """

    __tablename__ = "trajectories"

    trajectory_id: Mapped[str] = uuid_pk()

    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    trace_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    success_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    parent_trajectory_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("trajectories.trajectory_id", ondelete="SET NULL"), nullable=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    feedback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_relearning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    relearning_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_feedback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    path: Mapped[str | None] = mapped_column(Text, nullable=True)

    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    parent_trajectory: Mapped[TrajectoryModel | None] = relationship(
        "TrajectoryModel", remote_side=[trajectory_id], foreign_keys=[parent_trajectory_id]
    )

    __table_args__ = (
        Index("idx_traj_user", "user_id"),
        Index("idx_traj_agent", "agent_id"),
        Index("idx_traj_zone", "zone_id"),
        Index("idx_traj_status", "status"),
        Index("idx_traj_task_type", "task_type"),
        Index("idx_traj_completed", "completed_at"),
        Index("idx_traj_relearning", "needs_relearning", "relearning_priority"),
        Index("idx_traj_path", "path"),
        Index("idx_traj_session", "session_id"),
        Index("idx_traj_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<TrajectoryModel(trajectory_id={self.trajectory_id}, status={self.status}, task={self.task_description[:50]})>"

    def validate(self) -> None:
        """Validate trajectory model before database operations."""
        if not self.user_id:
            raise ValidationError("user_id is required")
        if not self.task_description:
            raise ValidationError("task_description is required")
        if not self.trace_hash:
            raise ValidationError("trace_hash is required")
        valid_statuses = ["success", "failure", "partial"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")
        if self.success_score is not None and not 0.0 <= self.success_score <= 1.0:
            raise ValidationError(
                f"success_score must be between 0.0 and 1.0, got {self.success_score}"
            )


class TrajectoryFeedbackModel(Base):
    """Dynamic feedback for trajectories.

    Allows adding feedback to completed trajectories for production monitoring,
    human ratings, A/B test outcomes, and long-term metrics.
    """

    __tablename__ = "trajectory_feedback"

    feedback_id: Mapped[str] = uuid_pk()

    trajectory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trajectories.trajectory_id", ondelete="CASCADE"),
        nullable=False,
    )

    feedback_type: Mapped[str] = mapped_column(String(50), nullable=False)
    revised_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_feedback_trajectory", "trajectory_id"),
        Index("idx_feedback_type", "feedback_type"),
        Index("idx_feedback_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<TrajectoryFeedbackModel(feedback_id={self.feedback_id}, trajectory_id={self.trajectory_id}, type={self.feedback_type})>"


class PlaybookModel(Base):
    """Playbook storage for ACE (Agentic Context Engineering).

    Stores learned strategies and patterns for agents.
    """

    __tablename__ = "playbooks"

    playbook_id: Mapped[str] = uuid_pk()

    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_improvement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    scope: Mapped[str] = mapped_column(String(50), nullable=False, default="agent")
    visibility: Mapped[str] = mapped_column(String(50), nullable=False, default="private")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    path: Mapped[str | None] = mapped_column(Text, nullable=True)

    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("agent_id", "name", "version", name="uq_playbook_agent_name_version"),
        Index("idx_playbook_user", "user_id"),
        Index("idx_playbook_agent", "agent_id"),
        Index("idx_playbook_zone", "zone_id"),
        Index("idx_playbook_name", "name"),
        Index("idx_playbook_scope", "scope"),
        Index("idx_playbook_path", "path"),
        Index("idx_playbook_session", "session_id"),
        Index("idx_playbook_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<PlaybookModel(playbook_id={self.playbook_id}, name={self.name}, version={self.version})>"

    def validate(self) -> None:
        """Validate playbook model before database operations."""
        if not self.user_id:
            raise ValidationError("user_id is required")
        if not self.name:
            raise ValidationError("name is required")
        if self.version is not None and self.version < 1:
            raise ValidationError(f"version must be >= 1, got {self.version}")
        if not self.content_hash:
            raise ValidationError("content_hash is required")
        valid_scopes = ["agent", "user", "zone", "global"]
        if self.scope not in valid_scopes:
            raise ValidationError(f"scope must be one of {valid_scopes}, got {self.scope}")
        valid_visibilities = ["private", "shared", "public"]
        if self.visibility not in valid_visibilities:
            raise ValidationError(
                f"visibility must be one of {valid_visibilities}, got {self.visibility}"
            )
        if self.success_rate is not None and not 0.0 <= self.success_rate <= 1.0:
            raise ValidationError(
                f"success_rate must be between 0.0 and 1.0, got {self.success_rate}"
            )
        if self.usage_count is not None and self.usage_count < 0:
            raise ValidationError(f"usage_count must be non-negative, got {self.usage_count}")
