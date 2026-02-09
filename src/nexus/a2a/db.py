"""SQLAlchemy model for A2A task persistence.

Stores A2A task state, messages, artifacts, and push notification configs
in the same database used by the rest of Nexus (PostgreSQL or SQLite).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models import Base


def _generate_task_id() -> str:
    """Generate a UUID string for task IDs."""
    return str(uuid.uuid4())


class A2ATaskModel(Base):
    """Persistent storage for A2A tasks."""

    __tablename__ = "a2a_tasks"

    # Primary key
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_task_id)

    # Context for multi-turn conversations
    context_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Multi-tenant isolation
    zone_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Task state (TaskState enum value)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")

    # Messages (JSON array of Message dicts)
    messages_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # Artifacts (JSON array of Artifact dicts)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # Task metadata (JSON object)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Push notification configs (JSON array)
    push_configs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Most common query: list tasks filtered by zone + agent + state
        Index("ix_a2a_tasks_zone_agent_state", "zone_id", "agent_id", "state"),
        # Multi-turn conversation lookups
        Index("ix_a2a_tasks_context_id", "context_id"),
        # Pagination by creation time
        Index("ix_a2a_tasks_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<A2ATask id={self.id!r} state={self.state!r} zone={self.zone_id!r}>"
