"""ORM-to-domain converters for governance models (Issue #2129).

Centralises the ``_*_model_to_domain()`` helpers previously scattered
across ``anomaly_service.py``, ``governance_graph_service.py``, and
``response_service.py``.
"""

from __future__ import annotations

from typing import Any

from nexus.bricks.governance.json_utils import parse_json_metadata
from nexus.bricks.governance.models import (
    AnomalyAlert,
    AnomalySeverity,
    EdgeType,
    GovernanceEdge,
    SuspensionRecord,
)


def alert_model_to_domain(model: Any) -> AnomalyAlert:
    """Convert ``AnomalyAlertModel`` to domain ``AnomalyAlert``."""
    details = parse_json_metadata(getattr(model, "details", None))

    return AnomalyAlert(
        alert_id=model.id,
        agent_id=model.agent_id,
        zone_id=model.zone_id,
        severity=AnomalySeverity(model.severity),
        alert_type=model.alert_type,
        details=details,
        transaction_ref=model.transaction_ref,
        created_at=model.created_at,
        resolved=model.resolved,
        resolved_at=model.resolved_at,
        resolved_by=model.resolved_by,
    )


def edge_model_to_domain(model: Any) -> GovernanceEdge:
    """Convert ``GovernanceEdgeModel`` to domain ``GovernanceEdge``."""
    metadata = parse_json_metadata(getattr(model, "metadata_json", None))

    return GovernanceEdge(
        edge_id=model.id,
        from_node=model.from_node,
        to_node=model.to_node,
        zone_id=model.zone_id,
        edge_type=EdgeType(model.edge_type),
        weight=model.weight,
        metadata=metadata,
        created_at=model.created_at,
    )


def suspension_model_to_domain(model: Any) -> SuspensionRecord:
    """Convert ``SuspensionModel`` to domain ``SuspensionRecord``."""
    return SuspensionRecord(
        suspension_id=model.id,
        agent_id=model.agent_id,
        zone_id=model.zone_id,
        reason=model.reason,
        severity=AnomalySeverity(model.severity),
        suspended_at=model.suspended_at,
        expires_at=model.expires_at,
        appeal_status=model.appeal_status,
        appeal_reason=model.appeal_reason,
        appealed_at=model.appealed_at,
        decided_by=model.decided_by,
        decided_at=model.decided_at,
    )
