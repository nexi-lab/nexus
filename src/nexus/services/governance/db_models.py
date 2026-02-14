"""SQLAlchemy ORM models for governance tables.

Issue #1359: DB models for anomaly alerts, agent baselines,
governance graph nodes/edges, fraud rings/scores, suspensions, and throttles.

These are the persistence layer. Domain models live in models.py.
Services convert between these and domain models at the boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, TimestampMixin, ZoneIsolationMixin, uuid_pk

# =============================================================================
# Phase 1: Anomaly Detection Tables
# =============================================================================


class AnomalyAlertModel(Base, TimestampMixin, ZoneIsolationMixin):
    """Anomaly alert records."""

    __tablename__ = "governance_anomaly_alerts"

    id: Mapped[str] = uuid_pk()
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    transaction_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_gov_alerts_zone_agent_created", "zone_id", "agent_id", "created_at"),
        Index("ix_gov_alerts_zone_severity_resolved", "zone_id", "severity", "resolved"),
    )


class AgentBaselineModel(Base, ZoneIsolationMixin):
    """Agent transaction baselines for anomaly detection."""

    __tablename__ = "governance_agent_baselines"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    stats: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# =============================================================================
# Phase 2: Collusion Detection Tables
# =============================================================================


class GovernanceNodeModel(Base, TimestampMixin, ZoneIsolationMixin):
    """Governance graph nodes."""

    __tablename__ = "governance_nodes"

    id: Mapped[str] = uuid_pk()
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    node_type: Mapped[str] = mapped_column(String(20), nullable=False, default="agent")
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (Index("ix_gov_nodes_zone_agent", "zone_id", "agent_id", unique=True),)


class GovernanceEdgeModel(Base, TimestampMixin, ZoneIsolationMixin):
    """Governance graph edges."""

    __tablename__ = "governance_edges"

    id: Mapped[str] = uuid_pk()
    from_node: Mapped[str] = mapped_column(String(255), nullable=False)
    to_node: Mapped[str] = mapped_column(String(255), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(20), nullable=False, default="transaction")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_gov_edges_zone_from_to", "zone_id", "from_node", "to_node", unique=False),
        Index("ix_gov_edges_zone_type", "zone_id", "edge_type"),
    )


class FraudRingModel(Base, ZoneIsolationMixin):
    """Detected fraud rings."""

    __tablename__ = "governance_fraud_rings"

    id: Mapped[str] = uuid_pk()
    agents: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    ring_type: Mapped[str] = mapped_column(String(30), nullable=False, default="simple_cycle")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class FraudScoreModel(Base, ZoneIsolationMixin):
    """Per-agent composite fraud scores."""

    __tablename__ = "governance_fraud_scores"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    components: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


# =============================================================================
# Phase 4: Response Action Tables
# =============================================================================


class SuspensionModel(Base, TimestampMixin, ZoneIsolationMixin):
    """Agent suspension records."""

    __tablename__ = "governance_suspensions"

    id: Mapped[str] = uuid_pk()
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="high")
    suspended_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    appeal_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    appeal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    appealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_gov_suspensions_zone_agent", "zone_id", "agent_id"),)


class ThrottleModel(Base, TimestampMixin, ZoneIsolationMixin):
    """Agent throttle configurations."""

    __tablename__ = "governance_throttles"

    id: Mapped[str] = uuid_pk()
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_gov_throttles_zone_agent", "zone_id", "agent_id"),)
