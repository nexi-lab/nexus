"""Unit tests for governance domain models.

Tests that all frozen dataclasses are immutable, enum values are correct,
and default field factories work properly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus.services.governance.models import (
    AgentBaseline,
    AnomalyAlert,
    AnomalyDetectionConfig,
    AnomalySeverity,
    ConstraintCheckResult,
    ConstraintType,
    EdgeType,
    FraudRing,
    FraudScore,
    GovernanceEdge,
    GovernanceNode,
    NodeType,
    RingType,
    SuspensionRecord,
    ThrottleConfig,
    TransactionSummary,
)

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


class TestEnums:
    """Tests for governance enum values."""

    def test_anomaly_severity_values(self) -> None:
        assert AnomalySeverity.LOW == "low"
        assert AnomalySeverity.MEDIUM == "medium"
        assert AnomalySeverity.HIGH == "high"
        assert AnomalySeverity.CRITICAL == "critical"

    def test_node_type_values(self) -> None:
        assert NodeType.AGENT == "agent"
        assert NodeType.PRINCIPAL == "principal"

    def test_edge_type_values(self) -> None:
        assert EdgeType.TRANSACTION == "transaction"
        assert EdgeType.DELEGATION == "delegation"
        assert EdgeType.CONSTRAINT == "constraint"

    def test_constraint_type_values(self) -> None:
        assert ConstraintType.BLOCK == "block"
        assert ConstraintType.REQUIRE_APPROVAL == "require_approval"
        assert ConstraintType.RATE_LIMIT == "rate_limit"

    def test_ring_type_values(self) -> None:
        assert RingType.SIMPLE_CYCLE == "simple_cycle"
        assert RingType.COMPLEX_CYCLE == "complex_cycle"
        assert RingType.SYBIL_CLUSTER == "sybil_cluster"


# ---------------------------------------------------------------------------
# Frozen dataclass immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """Tests that frozen dataclasses cannot be mutated."""

    def test_transaction_summary_frozen(self) -> None:
        ts = TransactionSummary("a1", "z1", 100.0, "cp1", datetime.now(UTC))
        with pytest.raises(AttributeError):
            ts.amount = 999.0  # type: ignore[misc]

    def test_agent_baseline_frozen(self) -> None:
        bl = AgentBaseline("a1", "z1", 100.0, 10.0, 5.0, 3, datetime.now(UTC), 50)
        with pytest.raises(AttributeError):
            bl.mean_amount = 999.0  # type: ignore[misc]

    def test_anomaly_alert_frozen(self) -> None:
        alert = AnomalyAlert("id1", "a1", "z1", AnomalySeverity.HIGH, "amount")
        with pytest.raises(AttributeError):
            alert.severity = AnomalySeverity.LOW  # type: ignore[misc]

    def test_governance_edge_frozen(self) -> None:
        edge = GovernanceEdge("e1", "a", "b", "z1")
        with pytest.raises(AttributeError):
            edge.weight = 99.0  # type: ignore[misc]

    def test_fraud_ring_frozen(self) -> None:
        ring = FraudRing("r1", "z1", ["a", "b", "c"])
        with pytest.raises(AttributeError):
            ring.confidence = 0.99  # type: ignore[misc]

    def test_fraud_score_frozen(self) -> None:
        score = FraudScore("a1", "z1", 0.5)
        with pytest.raises(AttributeError):
            score.score = 0.99  # type: ignore[misc]

    def test_constraint_check_result_frozen(self) -> None:
        result = ConstraintCheckResult(allowed=True)
        with pytest.raises(AttributeError):
            result.allowed = False  # type: ignore[misc]

    def test_suspension_record_frozen(self) -> None:
        sr = SuspensionRecord("s1", "a1", "z1", "test")
        with pytest.raises(AttributeError):
            sr.reason = "modified"  # type: ignore[misc]

    def test_throttle_config_frozen(self) -> None:
        tc = ThrottleConfig("a1", "z1", 10, 100.0)
        with pytest.raises(AttributeError):
            tc.max_tx_per_hour = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaults:
    """Tests for default field values."""

    def test_anomaly_detection_config_defaults(self) -> None:
        config = AnomalyDetectionConfig()
        assert config.z_score_threshold == 3.0
        assert config.iqr_multiplier == 1.5
        assert config.min_observations == 10

    def test_governance_edge_defaults(self) -> None:
        edge = GovernanceEdge("e1", "a", "b", "z1")
        assert edge.edge_type == EdgeType.TRANSACTION
        assert edge.weight == 1.0
        assert edge.metadata == {}
        assert edge.created_at is None

    def test_governance_node_defaults(self) -> None:
        node = GovernanceNode("n1", "a1", "z1")
        assert node.node_type == NodeType.AGENT
        assert node.metadata == {}

    def test_anomaly_alert_defaults(self) -> None:
        alert = AnomalyAlert("id", "a1", "z1", AnomalySeverity.LOW, "amount")
        assert alert.details == {}
        assert alert.transaction_ref is None
        assert alert.resolved is False
        assert alert.resolved_at is None
        assert alert.resolved_by is None

    def test_constraint_check_defaults(self) -> None:
        result = ConstraintCheckResult(allowed=True)
        assert result.constraint_type is None
        assert result.reason is None
        assert result.edge_id is None

    def test_suspension_record_defaults(self) -> None:
        sr = SuspensionRecord("s1", "a1", "z1", "reason")
        assert sr.severity == AnomalySeverity.HIGH
        assert sr.appeal_status == "none"
        assert sr.appeal_reason is None
