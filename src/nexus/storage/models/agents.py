"""Agent lifecycle and delegation models.

Issue #1286: Extracted from monolithic __init__.py.
Issue #1271: Added DelegationRecordModel for agent delegation.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


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
    """Delegation record for agent identity delegation (Issue #1271, #1618).

    Tracks coordinator → worker agent delegation relationships,
    including the delegation mode, scope constraints, lease expiry,
    lifecycle status, chain tracking, and intent.

    JSON text columns store variable-length lists (paths) and scope objects.
    New columns (#1618) have defaults for backward compatibility with existing rows.
    """

    __tablename__ = "delegation_records"

    delegation_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    delegation_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    scope_prefix: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_grants: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_grants: Mapped[str | None] = mapped_column(Text, nullable=True)
    readonly_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    intent: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parent_delegation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_sub_delegate: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_delegation_agent_id", "agent_id"),
        Index("idx_delegation_parent_agent_id", "parent_agent_id"),
        Index("idx_delegation_lease_expires", "lease_expires_at"),
        Index("idx_delegation_status", "status"),
        Index("idx_delegation_parent_delegation", "parent_delegation_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DelegationRecordModel(delegation_id={self.delegation_id}, "
            f"agent_id={self.agent_id}, parent={self.parent_agent_id}, "
            f"mode={self.delegation_mode}, status={self.status})>"
        )
