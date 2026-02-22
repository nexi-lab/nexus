"""Governance domain models — frozen dataclasses (no ORM dependency).

Issue #1359: All data structures for anomaly detection, collusion detection,
governance graphs, and response actions.

Architecture:
    These are the domain layer. DB models live in db_models.py.
    Services operate on these; never pass ORM models outside service boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# =============================================================================
# Enums
# =============================================================================


class AnomalySeverity(StrEnum):
    """Alert severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NodeType(StrEnum):
    """Governance graph node types."""

    AGENT = "agent"
    PRINCIPAL = "principal"


class EdgeType(StrEnum):
    """Governance graph edge types."""

    TRANSACTION = "transaction"
    DELEGATION = "delegation"
    CONSTRAINT = "constraint"


class ConstraintType(StrEnum):
    """Types of governance constraints between agents."""

    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"
    RATE_LIMIT = "rate_limit"


class RingType(StrEnum):
    """Types of detected fraud rings."""

    SIMPLE_CYCLE = "simple_cycle"
    COMPLEX_CYCLE = "complex_cycle"
    SYBIL_CLUSTER = "sybil_cluster"


# =============================================================================
# Phase 1: Anomaly Detection Models
# =============================================================================


@dataclass(frozen=True)
class TransactionSummary:
    """Lightweight transaction snapshot for anomaly analysis."""

    agent_id: str
    zone_id: str
    amount: float
    counterparty: str
    timestamp: datetime


@dataclass(frozen=True)
class AgentBaseline:
    """Statistical baseline for an agent's transaction patterns."""

    agent_id: str
    zone_id: str
    mean_amount: float
    std_amount: float
    mean_frequency: float  # transactions per day
    counterparty_count: int
    computed_at: datetime
    observation_count: int = 0


@dataclass(frozen=True)
class AnomalyAlert:
    """Alert raised by anomaly detection."""

    alert_id: str
    agent_id: str
    zone_id: str
    severity: AnomalySeverity
    alert_type: str  # "amount", "frequency", "counterparty"
    details: dict[str, object] = field(default_factory=dict)
    transaction_ref: str | None = None
    created_at: datetime | None = None
    resolved: bool = False
    resolved_at: datetime | None = None
    resolved_by: str | None = None


@dataclass(frozen=True)
class AnomalyDetectionConfig:
    """Configuration for statistical anomaly detection."""

    z_score_threshold: float = 3.0
    iqr_multiplier: float = 1.5
    min_observations: int = 10


# =============================================================================
# Phase 2: Collusion Detection Models
# =============================================================================


@dataclass(frozen=True)
class GovernanceNode:
    """Node in the governance graph."""

    node_id: str
    agent_id: str
    zone_id: str
    node_type: NodeType = NodeType.AGENT
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True)
class GovernanceEdge:
    """Edge in the governance graph."""

    edge_id: str
    from_node: str
    to_node: str
    zone_id: str
    edge_type: EdgeType = EdgeType.TRANSACTION
    weight: float = 1.0
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True)
class FraudRing:
    """Detected fraud ring (cycle of colluding agents)."""

    ring_id: str
    zone_id: str
    agents: list[str]
    ring_type: RingType = RingType.SIMPLE_CYCLE
    confidence: float = 0.0  # 0.0 to 1.0
    total_volume: float = 0.0
    detected_at: datetime | None = None


@dataclass(frozen=True)
class FraudScore:
    """Composite fraud score for an agent."""

    agent_id: str
    zone_id: str
    score: float  # 0.0 (clean) to 1.0 (fraudulent)
    components: dict[str, float] = field(default_factory=dict)
    computed_at: datetime | None = None


# =============================================================================
# Phase 3: Governance Graph Models
# =============================================================================


@dataclass(frozen=True)
class ConstraintCheckResult:
    """Result of checking governance constraints between two agents."""

    allowed: bool
    constraint_type: ConstraintType | None = None
    reason: str | None = None
    edge_id: str | None = None


# =============================================================================
# Phase 4: Response Action Models
# =============================================================================


@dataclass(frozen=True)
class SuspensionRecord:
    """Record of an agent suspension."""

    suspension_id: str
    agent_id: str
    zone_id: str
    reason: str
    severity: AnomalySeverity = AnomalySeverity.HIGH
    suspended_at: datetime | None = None
    expires_at: datetime | None = None
    appeal_status: str = "none"  # none | pending | approved | rejected
    appeal_reason: str | None = None
    appealed_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class ThrottleConfig:
    """Rate throttle applied to an agent."""

    agent_id: str
    zone_id: str
    max_tx_per_hour: int
    max_amount_per_day: float
    reason: str = ""
    applied_at: datetime | None = None
    expires_at: datetime | None = None
