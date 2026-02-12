"""ReputationEventModel — append-only reputation event log.

Issue #1356 Phase 2: Every feedback, dispute outcome, or penalty is recorded
as an immutable event for event-sourced reputation scoring.

Design decisions:
- Composite PK (id, created_at) for future time-based partitioning.
- SHA-256 record_hash for tamper detection.
- String columns for enums (no PG ENUM) for forward compatibility.
- 4 strategic indexes for common query patterns.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class ReputationEventModel(Base):
    """Immutable reputation event (feedback, dispute outcome, penalty, etc.).

    Records are append-only: each event captures a single reputation-relevant
    interaction between two agents within an exchange.
    """

    __tablename__ = "reputation_events"

    # Composite primary key — partition-ready
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Self-hash for tamper detection (SHA-256 hex digest)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Participants
    rater_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rated_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    exchange_id: Mapped[str] = mapped_column(String(36), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default="default")

    # Event classification
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)

    # Per-dimension scores (0.0-1.0, nullable for non-feedback events)
    reliability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeliness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fairness_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Evidence + context
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context: Mapped[str] = mapped_column(String(100), nullable=False, default="general")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    __table_args__ = (
        # 1. Composite B-tree: rated agent lookups by time
        Index("idx_rep_event_rated_created", "rated_agent_id", "created_at"),
        # 2. Composite B-tree: rater agent lookups by time
        Index("idx_rep_event_rater_created", "rater_agent_id", "created_at"),
        # 3. Unique: one feedback per rater per exchange
        Index("idx_rep_event_exchange_rater", "exchange_id", "rater_agent_id", unique=True),
        # 4. BRIN: zone + time scans (append-only data)
        Index(
            "idx_rep_event_zone_created_brin",
            "zone_id",
            "created_at",
            postgresql_using="brin",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ReputationEvent(id={self.id!r}, rater={self.rater_agent_id!r}, "
            f"rated={self.rated_agent_id!r}, outcome={self.outcome!r})>"
        )
