"""Agent lifecycle and delegation models.

Issue #1286: Extracted from monolithic __init__.py.
Issue #1271: Added DelegationRecordModel for agent delegation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


class AgentRecordModel(Base):
    """Agent record for lifecycle tracking (Agent OS Phase 1, Issue #1240).

    Stores agent identity, lifecycle state, session generation counter,
    and heartbeat timestamps. Uses optimistic locking via the generation
    column for cross-DB (SQLite + PostgreSQL) concurrency control.
    """

    __tablename__ = "agent_records"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    agent_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_manifest: Mapped[str | None] = mapped_column(Text, nullable=True, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_agent_records_zone_state", "zone_id", "state"),
        Index("idx_agent_records_state_heartbeat", "state", "last_heartbeat"),
        Index("idx_agent_records_owner", "owner_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentRecordModel(agent_id={self.agent_id}, state={self.state}, "
            f"generation={self.generation})>"
        )


class AgentEventModel(Base):
    """Agent lifecycle events audit log (Issue #1307).

    Append-only table recording agent lifecycle events such as sandbox
    creation, connection, and termination.
    """

    __tablename__ = "agent_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("ix_agent_events_agent_created", "agent_id", "created_at"),
        Index("ix_agent_events_type", "event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentEventModel(id={self.id}, agent_id={self.agent_id}, "
            f"event_type={self.event_type})>"
        )


class DelegationRecordModel(Base):
    """Delegation record for agent identity delegation (Issue #1271).

    Tracks coordinator → worker agent delegation relationships,
    including the delegation mode, scope constraints, and lease expiry.
    JSON text columns store variable-length lists (paths).
    """

    __tablename__ = "delegation_records"

    delegation_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    delegation_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_prefix: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_grants: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_grants: Mapped[str | None] = mapped_column(Text, nullable=True)
    readonly_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_delegation_agent_id", "agent_id"),
        Index("idx_delegation_parent_agent_id", "parent_agent_id"),
        Index("idx_delegation_lease_expires", "lease_expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<DelegationRecordModel(delegation_id={self.delegation_id}, "
            f"agent_id={self.agent_id}, parent={self.parent_agent_id}, "
            f"mode={self.delegation_mode})>"
        )
