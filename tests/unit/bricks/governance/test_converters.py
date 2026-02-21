"""Unit tests for governance ORM-to-domain converters.

Tests alert_model_to_domain(), edge_model_to_domain(), and
suspension_model_to_domain() with mock ORM model objects.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from nexus.bricks.governance.converters import (
    alert_model_to_domain,
    edge_model_to_domain,
    suspension_model_to_domain,
)
from nexus.bricks.governance.models import (
    AnomalyAlert,
    AnomalySeverity,
    EdgeType,
    GovernanceEdge,
    SuspensionRecord,
)


def _make_alert_model(**overrides: object) -> SimpleNamespace:
    """Create a fake AnomalyAlertModel with sensible defaults."""
    now = datetime.now(UTC)
    defaults = {
        "id": "alert-1",
        "agent_id": "agent-a",
        "zone_id": "zone-1",
        "severity": "high",
        "alert_type": "amount",
        "details": None,
        "transaction_ref": "tx-ref-1",
        "created_at": now,
        "resolved": False,
        "resolved_at": None,
        "resolved_by": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_edge_model(**overrides: object) -> SimpleNamespace:
    """Create a fake GovernanceEdgeModel with sensible defaults."""
    now = datetime.now(UTC)
    defaults = {
        "id": "edge-1",
        "from_node": "node-a",
        "to_node": "node-b",
        "zone_id": "zone-1",
        "edge_type": "transaction",
        "weight": 1.0,
        "metadata_json": None,
        "created_at": now,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_suspension_model(**overrides: object) -> SimpleNamespace:
    """Create a fake SuspensionModel with sensible defaults."""
    now = datetime.now(UTC)
    defaults = {
        "id": "susp-1",
        "agent_id": "agent-a",
        "zone_id": "zone-1",
        "reason": "fraud detected",
        "severity": "high",
        "suspended_at": now,
        "expires_at": None,
        "appeal_status": "none",
        "appeal_reason": None,
        "appealed_at": None,
        "decided_by": None,
        "decided_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# alert_model_to_domain
# ---------------------------------------------------------------------------


class TestAlertModelToDomain:
    """Tests for alert_model_to_domain()."""

    def test_basic_conversion(self) -> None:
        model = _make_alert_model()
        result = alert_model_to_domain(model)

        assert isinstance(result, AnomalyAlert)
        assert result.alert_id == "alert-1"
        assert result.agent_id == "agent-a"
        assert result.zone_id == "zone-1"
        assert result.severity == AnomalySeverity.HIGH
        assert result.alert_type == "amount"
        assert result.transaction_ref == "tx-ref-1"
        assert result.resolved is False

    def test_details_parsed_from_json(self) -> None:
        model = _make_alert_model(details='{"z_score": 5.2}')
        result = alert_model_to_domain(model)
        assert result.details == {"z_score": 5.2}

    def test_details_none_yields_empty_dict(self) -> None:
        model = _make_alert_model(details=None)
        result = alert_model_to_domain(model)
        assert result.details == {}

    def test_details_malformed_json_yields_empty_dict(self) -> None:
        model = _make_alert_model(details="{bad json}")
        result = alert_model_to_domain(model)
        assert result.details == {}

    def test_resolved_alert(self) -> None:
        now = datetime.now(UTC)
        model = _make_alert_model(resolved=True, resolved_at=now, resolved_by="admin")
        result = alert_model_to_domain(model)
        assert result.resolved is True
        assert result.resolved_at == now
        assert result.resolved_by == "admin"

    def test_all_severity_levels(self) -> None:
        for sev in ("low", "medium", "high", "critical"):
            model = _make_alert_model(severity=sev)
            result = alert_model_to_domain(model)
            assert result.severity == AnomalySeverity(sev)


# ---------------------------------------------------------------------------
# edge_model_to_domain
# ---------------------------------------------------------------------------


class TestEdgeModelToDomain:
    """Tests for edge_model_to_domain()."""

    def test_basic_conversion(self) -> None:
        model = _make_edge_model()
        result = edge_model_to_domain(model)

        assert isinstance(result, GovernanceEdge)
        assert result.edge_id == "edge-1"
        assert result.from_node == "node-a"
        assert result.to_node == "node-b"
        assert result.zone_id == "zone-1"
        assert result.edge_type == EdgeType.TRANSACTION
        assert result.weight == 1.0

    def test_metadata_parsed_from_json(self) -> None:
        model = _make_edge_model(metadata_json='{"constraint_type": "block"}')
        result = edge_model_to_domain(model)
        assert result.metadata == {"constraint_type": "block"}

    def test_metadata_none_yields_empty_dict(self) -> None:
        model = _make_edge_model(metadata_json=None)
        result = edge_model_to_domain(model)
        assert result.metadata == {}

    def test_delegation_edge_type(self) -> None:
        model = _make_edge_model(edge_type="delegation")
        result = edge_model_to_domain(model)
        assert result.edge_type == EdgeType.DELEGATION

    def test_constraint_edge_type(self) -> None:
        model = _make_edge_model(edge_type="constraint")
        result = edge_model_to_domain(model)
        assert result.edge_type == EdgeType.CONSTRAINT

    def test_custom_weight(self) -> None:
        model = _make_edge_model(weight=3.5)
        result = edge_model_to_domain(model)
        assert result.weight == 3.5


# ---------------------------------------------------------------------------
# suspension_model_to_domain
# ---------------------------------------------------------------------------


class TestSuspensionModelToDomain:
    """Tests for suspension_model_to_domain()."""

    def test_basic_conversion(self) -> None:
        model = _make_suspension_model()
        result = suspension_model_to_domain(model)

        assert isinstance(result, SuspensionRecord)
        assert result.suspension_id == "susp-1"
        assert result.agent_id == "agent-a"
        assert result.zone_id == "zone-1"
        assert result.reason == "fraud detected"
        assert result.severity == AnomalySeverity.HIGH

    def test_appeal_fields_populated(self) -> None:
        now = datetime.now(UTC)
        model = _make_suspension_model(
            appeal_status="pending",
            appeal_reason="false positive",
            appealed_at=now,
        )
        result = suspension_model_to_domain(model)
        assert result.appeal_status == "pending"
        assert result.appeal_reason == "false positive"
        assert result.appealed_at == now

    def test_decided_appeal(self) -> None:
        now = datetime.now(UTC)
        model = _make_suspension_model(
            appeal_status="approved",
            decided_by="admin",
            decided_at=now,
        )
        result = suspension_model_to_domain(model)
        assert result.appeal_status == "approved"
        assert result.decided_by == "admin"
        assert result.decided_at == now

    def test_expires_at_set(self) -> None:
        now = datetime.now(UTC)
        model = _make_suspension_model(expires_at=now)
        result = suspension_model_to_domain(model)
        assert result.expires_at == now

    def test_all_severity_levels(self) -> None:
        for sev in ("low", "medium", "high", "critical"):
            model = _make_suspension_model(severity=sev)
            result = suspension_model_to_domain(model)
            assert result.severity == AnomalySeverity(sev)
