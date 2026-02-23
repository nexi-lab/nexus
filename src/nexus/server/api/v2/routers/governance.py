"""Governance REST API endpoints.

Issue #1359: Anti-fraud & anti-collusion governance graphs.

Provides endpoints for:
- Anomaly Detection: List/resolve alerts
- Collusion: Fraud scores, detected rings
- Governance Graph: Constraint CRUD, constraint checks
- Response Actions: Suspensions, appeals
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v2/governance", tags=["governance"], dependencies=[Depends(require_admin)]
)

# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


class AddConstraintRequest(BaseModel):
    """Request to add a governance constraint."""

    from_agent: str = Field(..., description="Source agent ID")
    to_agent: str = Field(..., description="Target agent ID")
    zone_id: str = Field(default=ROOT_ZONE_ID, description="Zone ID")
    constraint_type: str = Field(default="block", description="block, require_approval, rate_limit")
    reason: str = Field(default="", description="Reason for constraint")


class SuspendAgentRequest(BaseModel):
    """Request to suspend an agent."""

    agent_id: str = Field(..., description="Agent to suspend")
    zone_id: str = Field(default=ROOT_ZONE_ID, description="Zone ID")
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
    zone_id: str = Query(default=ROOT_ZONE_ID),
    severity: str | None = Query(default=None),
    resolved: bool | None = Query(default=None),
) -> JSONResponse:
    """List anomaly alerts with optional filters."""
    service = _get_anomaly_service(request)

    from nexus.bricks.governance.models import AnomalySeverity

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
    zone_id: str = Query(default=ROOT_ZONE_ID),
    auth_result: dict = Depends(require_admin),
) -> JSONResponse:
    """Resolve an anomaly alert."""
    logger.info("resolve_alert by subject=%s", auth_result.get("subject_id"))
    service = _get_anomaly_service(request)
    alert = await service.resolve_alert(
        alert_id=alert_id, resolved_by=body.resolved_by, zone_id=zone_id
    )

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
    zone_id: str = Query(default=ROOT_ZONE_ID),
) -> JSONResponse:
    """List stored fraud scores for all agents in a zone.

    Returns pre-computed scores from the database.
    Use POST /fraud-scores/compute to trigger recomputation.
    """
    service = _get_collusion_service(request)
    scores = await service.list_fraud_scores(zone_id=zone_id)

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
                for s in scores
            ],
            "count": len(scores),
        }
    )


@router.post("/fraud-scores/compute")
async def compute_fraud_scores(
    request: Request,
    zone_id: str = Query(default=ROOT_ZONE_ID),
    auth_result: dict = Depends(require_admin),
) -> JSONResponse:
    """Recompute and persist fraud scores for all agents in a zone."""
    logger.info("compute_fraud_scores by subject=%s", auth_result.get("subject_id"))
    service = _get_collusion_service(request)
    scores = await service.compute_and_persist_fraud_scores(zone_id=zone_id)

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
    zone_id: str = Query(default=ROOT_ZONE_ID),
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
    zone_id: str = Query(default=ROOT_ZONE_ID),
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
    auth_result: dict = Depends(require_admin),
) -> JSONResponse:
    """Add a governance constraint between two agents."""
    logger.info("add_constraint by subject=%s", auth_result.get("subject_id"))
    service = _get_graph_service(request)

    from nexus.bricks.governance.models import ConstraintType

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
    zone_id: str = Query(default=ROOT_ZONE_ID),
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
    zone_id: str = Query(default=ROOT_ZONE_ID),
) -> JSONResponse:
    """Remove a governance constraint."""
    service = _get_graph_service(request)
    removed = await service.remove_constraint(edge_id, zone_id=zone_id)

    if not removed:
        raise HTTPException(status_code=404, detail=f"Constraint {edge_id} not found")

    return JSONResponse(content={"removed": True, "edge_id": edge_id})


@router.get("/check/{from_agent}/{to_agent}")
async def check_constraint(
    request: Request,
    from_agent: str,
    to_agent: str,
    zone_id: str = Query(default=ROOT_ZONE_ID),
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
    auth_result: dict = Depends(require_admin),
) -> JSONResponse:
    """Suspend an agent."""
    logger.info("suspend_agent by subject=%s", auth_result.get("subject_id"))
    service = _get_response_service(request)

    from nexus.bricks.governance.models import AnomalySeverity

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
    zone_id: str = Query(default=ROOT_ZONE_ID),
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
    zone_id: str = Query(default=ROOT_ZONE_ID),
) -> JSONResponse:
    """Appeal a suspension."""
    service = _get_response_service(request)

    try:
        record = await service.appeal_suspension(
            suspension_id=suspension_id,
            reason=body.reason,
            zone_id=zone_id,
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
    zone_id: str = Query(default=ROOT_ZONE_ID),
    auth_result: dict = Depends(require_admin),
) -> JSONResponse:
    """Decide on a suspension appeal."""
    logger.info("decide_appeal by subject=%s", auth_result.get("subject_id"))
    service = _get_response_service(request)

    try:
        record = await service.decide_appeal(
            suspension_id=suspension_id,
            approved=body.approved,
            decided_by=body.decided_by,
            zone_id=zone_id,
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


# =============================================================================
# Hotspot Detection Endpoints (#2056 — ported from v1 admin)
# =============================================================================


def _get_nexus_fs(request: Request) -> Any:
    """Get NexusFS instance from app.state."""
    fs = getattr(request.app.state, "nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return fs


@router.get("/hotspot-stats")
async def get_hotspot_stats(
    request: Request,
    _auth_result: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Get hotspot detection statistics (Issue #921)."""
    nexus_fs = _get_nexus_fs(request)
    permission_enforcer = getattr(nexus_fs, "_permission_enforcer", None)
    if not permission_enforcer:
        raise HTTPException(status_code=503, detail="Permission enforcer not available")

    hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)
    if not hotspot_detector:
        return {"enabled": False, "message": "Hotspot tracking not enabled"}

    stats: dict[str, Any] = hotspot_detector.get_stats()
    return stats


@router.get("/hot-entries")
async def get_hot_entries(
    request: Request,
    limit: int = Query(10, description="Maximum number of entries", ge=1, le=100),
    _auth_result: dict = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Get current hot permission entries (Issue #921)."""
    nexus_fs = _get_nexus_fs(request)
    permission_enforcer = getattr(nexus_fs, "_permission_enforcer", None)
    if not permission_enforcer:
        raise HTTPException(status_code=503, detail="Permission enforcer not available")

    hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)
    if not hotspot_detector:
        return []

    entries = hotspot_detector.get_hot_entries(limit=limit)
    return [
        {
            "subject_type": e.subject_type,
            "subject_id": e.subject_id,
            "resource_type": e.resource_type,
            "permission": e.permission,
            "zone_id": e.zone_id,
            "access_count": e.access_count,
            "last_access": e.last_access,
        }
        for e in entries
    ]
