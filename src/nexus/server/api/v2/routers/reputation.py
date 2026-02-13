"""Reputation & Trust REST API endpoints (Issue #1356).

Provides:
- GET  /agents/{agent_id}/reputation    — Get reputation score
- GET  /reputation/leaderboard          — Zone leaderboard
- POST /exchanges/{exchange_id}/feedback — Submit feedback
- GET  /exchanges/{exchange_id}/feedback — Get feedback for exchange
- POST /exchanges/{exchange_id}/dispute  — File dispute
- GET  /disputes/{dispute_id}            — Get dispute status
- POST /disputes/{dispute_id}/resolve    — Resolve dispute

All endpoints are authenticated via existing auth middleware.
All endpoints use sync ``def`` for threadpool dispatch (no async DB).

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nexus.server.api.v2.dependencies import get_reputation_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reputation"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ReputationScoreResponse(BaseModel):
    """Reputation score for an agent."""

    agent_id: str
    context: str
    window: str
    composite_score: float
    composite_confidence: float
    reliability_alpha: float
    reliability_beta: float
    quality_alpha: float
    quality_beta: float
    timeliness_alpha: float
    timeliness_beta: float
    fairness_alpha: float
    fairness_beta: float
    total_interactions: int
    positive_interactions: int
    negative_interactions: int
    disputed_interactions: int
    global_trust_score: float | None = None
    zone_id: str
    updated_at: datetime


class ReputationLeaderboardResponse(BaseModel):
    """Leaderboard of agents ranked by reputation."""

    entries: list[ReputationScoreResponse]


class FeedbackSubmitRequest(BaseModel):
    """Request to submit feedback for an exchange."""

    rater_agent_id: str
    rated_agent_id: str
    outcome: str = Field(description="positive, negative, neutral, or mixed")
    reliability_score: float | None = None
    quality_score: float | None = None
    timeliness_score: float | None = None
    fairness_score: float | None = None
    evidence_hash: str | None = None
    context: str = "general"


class FeedbackEventResponse(BaseModel):
    """A single feedback event."""

    id: str
    rater_agent_id: str
    rated_agent_id: str
    exchange_id: str
    zone_id: str
    event_type: str
    outcome: str
    reliability_score: float | None = None
    quality_score: float | None = None
    timeliness_score: float | None = None
    fairness_score: float | None = None
    evidence_hash: str | None = None
    context: str
    weight: float
    record_hash: str
    created_at: datetime


class FeedbackSubmitResponse(BaseModel):
    """Response after submitting feedback."""

    event: FeedbackEventResponse


class FeedbackListResponse(BaseModel):
    """List of feedback events for an exchange."""

    feedback: list[FeedbackEventResponse]


class DisputeFileRequest(BaseModel):
    """Request to file a dispute."""

    complainant_agent_id: str
    respondent_agent_id: str
    reason: str
    evidence_hash: str | None = None


class DisputeResponse(BaseModel):
    """Dispute details."""

    id: str
    exchange_id: str
    zone_id: str
    complainant_agent_id: str
    respondent_agent_id: str
    status: str
    tier: int
    reason: str
    resolution: str | None = None
    resolution_evidence_hash: str | None = None
    escrow_amount: str | None = None
    escrow_released: bool
    filed_at: datetime
    resolved_at: datetime | None = None
    appeal_deadline: datetime | None = None


class DisputeResolveRequest(BaseModel):
    """Request to resolve a dispute."""

    resolution: str
    evidence_hash: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _score_to_response(score: Any) -> ReputationScoreResponse:
    """Convert ReputationScore record to response model."""
    return ReputationScoreResponse(
        agent_id=score.agent_id,
        context=score.context,
        window=score.window,
        composite_score=score.composite_score,
        composite_confidence=score.composite_confidence,
        reliability_alpha=score.reliability_alpha,
        reliability_beta=score.reliability_beta,
        quality_alpha=score.quality_alpha,
        quality_beta=score.quality_beta,
        timeliness_alpha=score.timeliness_alpha,
        timeliness_beta=score.timeliness_beta,
        fairness_alpha=score.fairness_alpha,
        fairness_beta=score.fairness_beta,
        total_interactions=score.total_interactions,
        positive_interactions=score.positive_interactions,
        negative_interactions=score.negative_interactions,
        disputed_interactions=score.disputed_interactions,
        global_trust_score=score.global_trust_score,
        zone_id=score.zone_id,
        updated_at=score.updated_at,
    )


def _event_to_response(event: Any) -> FeedbackEventResponse:
    """Convert ReputationEvent record to response model."""
    return FeedbackEventResponse(
        id=event.id,
        rater_agent_id=event.rater_agent_id,
        rated_agent_id=event.rated_agent_id,
        exchange_id=event.exchange_id,
        zone_id=event.zone_id,
        event_type=event.event_type,
        outcome=event.outcome,
        reliability_score=event.reliability_score,
        quality_score=event.quality_score,
        timeliness_score=event.timeliness_score,
        fairness_score=event.fairness_score,
        evidence_hash=event.evidence_hash,
        context=event.context,
        weight=event.weight,
        record_hash=event.record_hash,
        created_at=event.created_at,
    )


def _dispute_to_response(dispute: Any) -> DisputeResponse:
    """Convert DisputeRecord to response model."""
    return DisputeResponse(
        id=dispute.id,
        exchange_id=dispute.exchange_id,
        zone_id=dispute.zone_id,
        complainant_agent_id=dispute.complainant_agent_id,
        respondent_agent_id=dispute.respondent_agent_id,
        status=dispute.status,
        tier=dispute.tier,
        reason=dispute.reason,
        resolution=dispute.resolution,
        resolution_evidence_hash=dispute.resolution_evidence_hash,
        escrow_amount=dispute.escrow_amount,
        escrow_released=dispute.escrow_released,
        filed_at=dispute.filed_at,
        resolved_at=dispute.resolved_at,
        appeal_deadline=dispute.appeal_deadline,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/v2/agents/{agent_id}/reputation")
def get_agent_reputation(
    agent_id: str,
    context: str = "general",
    window: str = "all_time",
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> ReputationScoreResponse:
    """Get reputation score for an agent."""
    reputation_service, _dispute_service, _auth_ctx = deps

    score = reputation_service.get_reputation(agent_id, context=context, window=window)
    if score is None:
        raise HTTPException(status_code=404, detail="Reputation score not found")

    return _score_to_response(score)


@router.get("/api/v2/reputation/leaderboard")
def get_leaderboard(
    zone_id: str = "default",
    context: str = "general",
    limit: int = 50,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> ReputationLeaderboardResponse:
    """Get reputation leaderboard for a zone."""
    reputation_service, _dispute_service, _auth_ctx = deps

    entries = reputation_service.get_leaderboard(zone_id=zone_id, context=context, limit=limit)
    return ReputationLeaderboardResponse(entries=[_score_to_response(e) for e in entries])


@router.post("/api/v2/exchanges/{exchange_id}/feedback")
def submit_feedback(
    exchange_id: str,
    request: FeedbackSubmitRequest,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> FeedbackSubmitResponse:
    """Submit feedback for an exchange."""
    from nexus.services.reputation.reputation_service import DuplicateFeedbackError

    reputation_service, _dispute_service, auth_ctx = deps
    zone_id = auth_ctx.get("zone_id", "default") or "default"

    try:
        event = reputation_service.submit_feedback(
            rater_agent_id=request.rater_agent_id,
            rated_agent_id=request.rated_agent_id,
            exchange_id=exchange_id,
            zone_id=zone_id,
            outcome=request.outcome,
            reliability_score=request.reliability_score,
            quality_score=request.quality_score,
            timeliness_score=request.timeliness_score,
            fairness_score=request.fairness_score,
            evidence_hash=request.evidence_hash,
            context=request.context,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except DuplicateFeedbackError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return FeedbackSubmitResponse(event=_event_to_response(event))


@router.get("/api/v2/exchanges/{exchange_id}/feedback")
def get_exchange_feedback(
    exchange_id: str,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> FeedbackListResponse:
    """Get all feedback for an exchange."""
    reputation_service, _dispute_service, _auth_ctx = deps

    events = reputation_service.get_feedback_for_exchange(exchange_id)
    return FeedbackListResponse(feedback=[_event_to_response(e) for e in events])


@router.post("/api/v2/exchanges/{exchange_id}/dispute")
def file_dispute(
    exchange_id: str,
    request: DisputeFileRequest,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> DisputeResponse:
    """File a dispute for an exchange."""
    from nexus.services.reputation.dispute_service import DuplicateDisputeError

    _reputation_service, dispute_service, auth_ctx = deps
    zone_id = auth_ctx.get("zone_id", "default") or "default"

    try:
        dispute = dispute_service.file_dispute(
            exchange_id=exchange_id,
            complainant_agent_id=request.complainant_agent_id,
            respondent_agent_id=request.respondent_agent_id,
            zone_id=zone_id,
            reason=request.reason,
            evidence_hash=request.evidence_hash,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except DuplicateDisputeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return _dispute_to_response(dispute)


@router.get("/api/v2/disputes/{dispute_id}")
def get_dispute(
    dispute_id: str,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> DisputeResponse:
    """Get dispute status by ID."""
    _reputation_service, dispute_service, _auth_ctx = deps

    dispute = dispute_service.get_dispute(dispute_id)
    if dispute is None:
        raise HTTPException(status_code=404, detail="Dispute not found")

    return _dispute_to_response(dispute)


@router.post("/api/v2/disputes/{dispute_id}/resolve")
def resolve_dispute(
    dispute_id: str,
    request: DisputeResolveRequest,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_reputation_context),
) -> DisputeResponse:
    """Resolve a dispute (admin/auto-mediation)."""
    from nexus.services.reputation.dispute_service import (
        DisputeNotFoundError,
        InvalidTransitionError,
    )

    _reputation_service, dispute_service, _auth_ctx = deps

    try:
        dispute = dispute_service.resolve(
            dispute_id=dispute_id,
            resolution=request.resolution,
            evidence_hash=request.evidence_hash,
        )
    except DisputeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return _dispute_to_response(dispute)
