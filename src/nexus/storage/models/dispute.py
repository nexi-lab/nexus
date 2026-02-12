"""DisputeModel — dispute lifecycle state machine.

Issue #1356 Phase 2: Tracks dispute resolution for exchange-related
disagreements between agents. State machine:

    filed → auto_mediating → resolved
                           → dismissed
    filed → dismissed

Design decisions:
- UUID PK (not composite — disputes are not partitioned by time).
- String columns for status (no PG ENUM) for forward compatibility.
- 3 strategic indexes for common query patterns.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class DisputeModel(Base):
    """Dispute lifecycle for exchange disagreements between agents.

    State machine: filed → auto_mediating → resolved | dismissed.
    """

    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Exchange + zone
    exchange_id: Mapped[str] = mapped_column(String(36), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default="default")

    # Parties
    complainant_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    respondent_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # State machine
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="filed")
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Dispute details
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_evidence_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # Escrow
    escrow_amount: Mapped[str | None] = mapped_column(String(50), nullable=True)
    escrow_released: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Timestamps
    filed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    appeal_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 1. Unique: one dispute per exchange
        Index("idx_dispute_exchange_id", "exchange_id", unique=True),
        # 2. Status + zone: active dispute queries
        Index("idx_dispute_status_zone", "status", "zone_id"),
        # 3. Party lookups
        Index(
            "idx_dispute_parties",
            "complainant_agent_id",
            "respondent_agent_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Dispute(id={self.id!r}, exchange={self.exchange_id!r}, "
            f"status={self.status!r})>"
        )
