"""Governance REST API endpoints.

Issue #1359: Anti-fraud & anti-collusion governance graphs.

Provides endpoints for:
- Anomaly Detection: List/resolve alerts
- Collusion: Fraud scores, detected rings
- Governance Graph: Constraint CRUD, constraint checks
- Response Actions: Suspensions, appeals
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/governance", tags=["governance"])


# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


class AddConstraintRequest(BaseModel):
    """Request to add a governance constraint."""

    from_agent: str = Field(..., description="Source agent ID")
    to_agent: str = Field(..., description="Target agent ID")
    zone_id: str = Field(default="default", description="Zone ID")
    constraint_type: str = Field(default="block", description="block, require_approval, rate_limit")
    reason: str = Field(default="", description="Reason for constraint")


class SuspendAgentRequest(BaseModel):
    """Request to suspend an agent."""

    agent_id: str = Field(..., description="Agent to suspend")
    zone_id: str = Field(default="default", description="Zone ID")
    reason: str = Field(..., description="Reason for suspension")
    duration_hours: float = Field(default=24.0, description="Suspension duration in hours")
    severity: str = Field(default="high", description="Severity level")


class AppealRequest(BaseModel):
    """Request to appeal a suspension."""

    reason: str = Field(..., description="Appeal reason")


class DecideAppealRequest(BaseModel):
    """Request to decide on an appeal."""

    approved: bool = Field(..., description="Whether to approve the appeal")
    decided_by: str = Field(..., description="ID of the decision maker")


class ResolveAlertRequest(BaseModel):
    """Request to resolve an alert."""

    resolved_by: str = Field(..., description="ID of the resolver")


# =============================================================================
# Helper: get services from app.state
# =============================================================================


def _get_anomaly_service(request: Request) -> Any:
    service = getattr(request.app.state, "governance_anomaly_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Governance anomaly service not available")
    return service


def _get_collusion_service(request: Request) -> Any:
    service = getattr(request.app.state, "governance_collusion_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Governance collusion service not available")
    return service


def _get_graph_service(request: Request) -> Any:
    service = getattr(request.app.state, "governance_graph_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Governance graph service not available")
    return service


def _get_response_service(request: Request) -> Any:
    service = getattr(request.app.state, "governance_response_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Governance response service not available")
    return service


# =============================================================================
# Anomaly Detection Endpoints
# =============================================================================


@router.get("/alerts")
async def list_alerts(
    request: Request,
    zone_id: str = Query(default="default"),
    severity: str | None = Query(default=None),
    resolved: bool | None = Query(default=None),
) -> JSONResponse:
    """List anomaly alerts with optional filters."""
    service = _get_anomaly_service(request)

    from nexus.services.governance.models import AnomalySeverity

    sev = AnomalySeverity(severity) if severity else None
    alerts = await service.get_alerts(zone_id=zone_id, severity=sev, resolved=resolved)

    return JSONResponse(
        content={
            "alerts": [
                {
                    "alert_id": a.alert_id,
                    "agent_id": a.agent_id,
                    "zone_id": a.zone_id,
                    "severity": a.severity,
                    "alert_type": a.alert_type,
                    "details": a.details,
                    "resolved": a.resolved,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in alerts
            ],
            "count": len(alerts),
        }
    )


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    request: Request,
    alert_id: str,
    body: ResolveAlertRequest,
) -> JSONResponse:
    """Resolve an anomaly alert."""
    service = _get_anomaly_service(request)
    alert = await service.resolve_alert(alert_id=alert_id, resolved_by=body.resolved_by)

    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    return JSONResponse(
        content={
            "alert_id": alert.alert_id,
            "resolved": alert.resolved,
            "resolved_by": alert.resolved_by,
        }
    )


# =============================================================================
# Collusion Detection Endpoints
# =============================================================================


@router.get("/fraud-scores")
async def list_fraud_scores(
    request: Request,
    zone_id: str = Query(default="default"),
) -> JSONResponse:
    """List fraud scores for all agents in a zone."""
    service = _get_collusion_service(request)
    scores = await service.compute_fraud_scores(zone_id=zone_id)

    return JSONResponse(
        content={
            "scores": [
                {
                    "agent_id": s.agent_id,
                    "zone_id": s.zone_id,
                    "score": s.score,
                    "components": s.components,
                    "computed_at": s.computed_at.isoformat() if s.computed_at else None,
                }
                for s in scores.values()
            ],
            "count": len(scores),
        }
    )


@router.get("/fraud-scores/{agent_id}")
async def get_fraud_score(
    request: Request,
    agent_id: str,
    zone_id: str = Query(default="default"),
) -> JSONResponse:
    """Get fraud score for a specific agent."""
    service = _get_collusion_service(request)
    score = await service.get_fraud_score(agent_id=agent_id, zone_id=zone_id)

    if score is None:
        raise HTTPException(status_code=404, detail=f"No fraud score for agent {agent_id}")

    return JSONResponse(
        content={
            "agent_id": score.agent_id,
            "zone_id": score.zone_id,
            "score": score.score,
            "components": score.components,
            "computed_at": score.computed_at.isoformat() if score.computed_at else None,
        }
    )


@router.get("/rings")
async def list_fraud_rings(
    request: Request,
    zone_id: str = Query(default="default"),
) -> JSONResponse:
    """List detected fraud rings in a zone."""
    service = _get_collusion_service(request)
    rings = await service.detect_rings(zone_id=zone_id)

    return JSONResponse(
        content={
            "rings": [
                {
                    "ring_id": r.ring_id,
                    "zone_id": r.zone_id,
                    "agents": r.agents,
                    "ring_type": r.ring_type,
                    "confidence": r.confidence,
                    "total_volume": r.total_volume,
                    "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                }
                for r in rings
            ],
            "count": len(rings),
        }
    )


# =============================================================================
# Governance Graph Endpoints
# =============================================================================


@router.post("/constraints")
async def add_constraint(
    request: Request,
    body: AddConstraintRequest,
) -> JSONResponse:
    """Add a governance constraint between two agents."""
    service = _get_graph_service(request)

    from nexus.services.governance.models import ConstraintType

    try:
        ct = ConstraintType(body.constraint_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid constraint_type: {body.constraint_type}",
        ) from None

    edge = await service.add_constraint(
        from_agent=body.from_agent,
        to_agent=body.to_agent,
        zone_id=body.zone_id,
        constraint_type=ct,
        reason=body.reason,
    )

    return JSONResponse(
        status_code=201,
        content={
            "edge_id": edge.edge_id,
            "from_node": edge.from_node,
            "to_node": edge.to_node,
            "zone_id": edge.zone_id,
        },
    )


@router.get("/constraints")
async def list_constraints(
    request: Request,
    zone_id: str = Query(default="default"),
    agent_id: str | None = Query(default=None),
) -> JSONResponse:
    """List governance constraints."""
    service = _get_graph_service(request)
    constraints = await service.list_constraints(zone_id=zone_id, agent_id=agent_id)

    return JSONResponse(
        content={
            "constraints": [
                {
                    "edge_id": c.edge_id,
                    "from_node": c.from_node,
                    "to_node": c.to_node,
                    "zone_id": c.zone_id,
                    "metadata": c.metadata,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in constraints
            ],
            "count": len(constraints),
        }
    )


@router.delete("/constraints/{edge_id}")
async def remove_constraint(
    request: Request,
    edge_id: str,
) -> JSONResponse:
    """Remove a governance constraint."""
    service = _get_graph_service(request)
    removed = await service.remove_constraint(edge_id)

    if not removed:
        raise HTTPException(status_code=404, detail=f"Constraint {edge_id} not found")

    return JSONResponse(content={"removed": True, "edge_id": edge_id})


@router.get("/check/{from_agent}/{to_agent}")
async def check_constraint(
    request: Request,
    from_agent: str,
    to_agent: str,
    zone_id: str = Query(default="default"),
) -> JSONResponse:
    """Check governance constraint between two agents."""
    service = _get_graph_service(request)
    result = await service.check_constraint(
        from_agent=from_agent,
        to_agent=to_agent,
        zone_id=zone_id,
    )

    return JSONResponse(
        content={
            "allowed": result.allowed,
            "constraint_type": result.constraint_type if result.constraint_type else None,
            "reason": result.reason,
            "edge_id": result.edge_id,
        }
    )


# =============================================================================
# Response Action Endpoints
# =============================================================================


@router.post("/suspensions")
async def suspend_agent(
    request: Request,
    body: SuspendAgentRequest,
) -> JSONResponse:
    """Suspend an agent."""
    service = _get_response_service(request)

    from nexus.services.governance.models import AnomalySeverity

    try:
        sev = AnomalySeverity(body.severity)
    except ValueError:
        sev = AnomalySeverity.HIGH

    record = await service.suspend_agent(
        agent_id=body.agent_id,
        zone_id=body.zone_id,
        reason=body.reason,
        duration_hours=body.duration_hours,
        severity=sev,
    )

    return JSONResponse(
        status_code=201,
        content={
            "suspension_id": record.suspension_id,
            "agent_id": record.agent_id,
            "zone_id": record.zone_id,
            "reason": record.reason,
            "suspended_at": record.suspended_at.isoformat() if record.suspended_at else None,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        },
    )


@router.get("/suspensions")
async def list_suspensions(
    request: Request,
    zone_id: str = Query(default="default"),
    agent_id: str | None = Query(default=None),
) -> JSONResponse:
    """List suspensions."""
    service = _get_response_service(request)
    records = await service.list_suspensions(zone_id=zone_id, agent_id=agent_id)

    return JSONResponse(
        content={
            "suspensions": [
                {
                    "suspension_id": r.suspension_id,
                    "agent_id": r.agent_id,
                    "zone_id": r.zone_id,
                    "reason": r.reason,
                    "severity": r.severity,
                    "appeal_status": r.appeal_status,
                    "suspended_at": r.suspended_at.isoformat() if r.suspended_at else None,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                }
                for r in records
            ],
            "count": len(records),
        }
    )


@router.post("/suspensions/{suspension_id}/appeal")
async def appeal_suspension(
    request: Request,
    suspension_id: str,
    body: AppealRequest,
) -> JSONResponse:
    """Appeal a suspension."""
    service = _get_response_service(request)

    try:
        record = await service.appeal_suspension(
            suspension_id=suspension_id,
            reason=body.reason,
        )
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Suspension {suspension_id} not found"
        ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return JSONResponse(
        content={
            "suspension_id": record.suspension_id,
            "appeal_status": record.appeal_status,
            "appeal_reason": record.appeal_reason,
        }
    )


@router.post("/suspensions/{suspension_id}/decide")
async def decide_appeal(
    request: Request,
    suspension_id: str,
    body: DecideAppealRequest,
) -> JSONResponse:
    """Decide on a suspension appeal."""
    service = _get_response_service(request)

    try:
        record = await service.decide_appeal(
            suspension_id=suspension_id,
            approved=body.approved,
            decided_by=body.decided_by,
        )
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Suspension {suspension_id} not found"
        ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return JSONResponse(
        content={
            "suspension_id": record.suspension_id,
            "appeal_status": record.appeal_status,
            "decided_by": record.decided_by,
        }
    )
