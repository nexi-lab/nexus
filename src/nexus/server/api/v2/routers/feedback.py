"""Feedback REST API endpoints.

Provides 5 endpoints for feedback management:
- POST /api/v2/feedback                  - Add feedback
- GET  /api/v2/feedback/queue            - Get relearning queue
- GET  /api/v2/feedback/{trajectory_id}  - Get feedback for trajectory
- POST /api/v2/feedback/score            - Calculate effective score
- POST /api/v2/feedback/relearn          - Mark for relearning
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from nexus.server.api.v2.models import (
    FeedbackAddRequest,
    FeedbackAddResponse,
    FeedbackRelearnRequest,
    FeedbackScoreRequest,
    FeedbackScoreResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/feedback", tags=["feedback"])


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_app_state() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_feedback_manager() -> Any:
    """Create FeedbackManager."""
    from nexus.core.ace.feedback import FeedbackManager

    app_state = _get_app_state()
    session = app_state.nexus_fs.memory.session
    return FeedbackManager(session=session)


# =============================================================================
# Endpoints
# Note: Static paths (/queue, /score, /relearn) must be defined BEFORE
# dynamic path (/{trajectory_id}) to ensure proper route matching.
# =============================================================================


@router.post("", response_model=FeedbackAddResponse, status_code=status.HTTP_201_CREATED)
async def add_feedback(
    request: FeedbackAddRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> FeedbackAddResponse:
    """Add feedback to a trajectory.

    Allows adding production feedback, human ratings, A/B test results,
    or monitoring alerts to completed trajectories.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        feedback_manager = _get_feedback_manager()

        feedback_id = feedback_manager.add_feedback(
            trajectory_id=request.trajectory_id,
            feedback_type=request.feedback_type,
            score=request.score,
            source=request.source,
            message=request.message,
            metrics=request.metrics,
        )

        return FeedbackAddResponse(feedback_id=feedback_id, status="created")

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Feedback add error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Feedback add error: {e}") from e


@router.get("/queue")
async def get_relearning_queue(
    limit: int = Query(10, ge=1, le=100, description="Maximum items to return"),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get trajectories marked for relearning.

    Returns trajectories in the relearning queue, ordered by priority.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        feedback_manager = _get_feedback_manager()
        queue = feedback_manager.get_relearning_queue(limit=limit)

        return {
            "queue": queue,
            "total": len(queue),
        }

    except Exception as e:
        logger.error(f"Relearning queue error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Relearning queue error: {e}") from e


@router.post("/score", response_model=FeedbackScoreResponse)
async def calculate_score(
    request: FeedbackScoreRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> FeedbackScoreResponse:
    """Calculate effective score for a trajectory.

    Computes the effective score using the specified strategy:
    - latest: Use most recent feedback score
    - average: Average of all feedback scores
    - weighted: Time-weighted average (recent scores weighted more)
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        feedback_manager = _get_feedback_manager()
        score = feedback_manager.get_effective_score(
            trajectory_id=request.trajectory_id,
            strategy=request.strategy,
        )

        return FeedbackScoreResponse(
            trajectory_id=request.trajectory_id,
            effective_score=score,
            strategy=request.strategy,
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Score calculation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Score calculation error: {e}") from e


@router.post("/relearn")
async def mark_for_relearning(
    request: FeedbackRelearnRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Mark a trajectory for relearning.

    Adds the trajectory to the relearning queue with a reason
    and priority level.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        feedback_manager = _get_feedback_manager()
        feedback_manager.mark_for_relearning(
            trajectory_id=request.trajectory_id,
            _reason=request.reason,
            priority=request.priority,
        )

        return {
            "trajectory_id": request.trajectory_id,
            "marked": True,
            "reason": request.reason,
            "priority": request.priority,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Relearn mark error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Relearn mark error: {e}") from e


# Dynamic path must be defined LAST to avoid matching static paths
@router.get("/{trajectory_id}")
async def get_trajectory_feedback(
    trajectory_id: str,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get all feedback for a trajectory.

    Returns the complete list of feedback entries for a trajectory.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        feedback_manager = _get_feedback_manager()
        feedbacks = feedback_manager.get_trajectory_feedback(trajectory_id)

        return {
            "trajectory_id": trajectory_id,
            "feedbacks": feedbacks,
            "total": len(feedbacks),
        }

    except Exception as e:
        logger.error(f"Feedback get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Feedback get error: {e}") from e
