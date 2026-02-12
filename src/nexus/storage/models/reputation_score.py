"""ReputationScoreModel — materialized reputation aggregate.

Issue #1356 Phase 2: Pre-computed Beta distribution parameters and composite
scores for fast reputation lookups. Updated incrementally on each feedback
event, with periodic full rebuilds for consistency.

Design decisions:
- Composite PK (agent_id, context, window) — one row per agent per context per window.
- Per-dimension Beta parameters (alpha/beta for reliability, quality, timeliness, fairness).
- Precomputed composite_score + composite_confidence for O(1) reads.
- Interaction counts for quick summary stats.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


class ReputationScoreModel(Base):
    """Materialized reputation score aggregate for an agent.

    One row per (agent_id, context, window) triple. Updated on each
    feedback event via incremental alpha/beta update, or via nightly rebuild.
    """

    __tablename__ = "reputation_scores"

    # Composite primary key
    agent_id: Mapped[str] = mapped_column(
        String(255), primary_key=True, nullable=False
    )
    context: Mapped[str] = mapped_column(
        String(100), primary_key=True, nullable=False, default="general"
    )
    window: Mapped[str] = mapped_column(
        String(20), primary_key=True, nullable=False, default="all_time"
    )

    # Per-dimension Beta distribution parameters (prior = 1.0)
    reliability_alpha: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reliability_beta: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    quality_alpha: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    quality_beta: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    timeliness_alpha: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    timeliness_beta: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    fairness_alpha: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    fairness_beta: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # Precomputed composite
    composite_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    composite_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Interaction counts
    total_interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positive_interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    negative_interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disputed_interactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Cross-zone trust
    global_trust_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Zone scope
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default="default")

    # Last update timestamp
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # 1. Leaderboard: zone + context, ordered by score
        Index(
            "idx_rep_score_leaderboard",
            "zone_id",
            "context",
            "composite_score",
        ),
        # 2. Trust queries: global trust score lookups
        Index("idx_rep_score_global_trust", "global_trust_score"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReputationScore(agent_id={self.agent_id!r}, context={self.context!r}, "
            f"window={self.window!r}, composite={self.composite_score:.3f})>"
        )
