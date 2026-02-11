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

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_feedback_manager,
)
from nexus.server.api.v2.models import (
    FeedbackAddRequest,
    FeedbackAddResponse,
    FeedbackRelearnRequest,
    FeedbackScoreRequest,
    FeedbackScoreResponse,
    TrajectoryFeedbackListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/feedback", tags=["feedback"])


# =============================================================================
# Endpoints
# Note: Static paths (/queue, /score, /relearn) must be defined BEFORE
# dynamic path (/{trajectory_id}) to ensure proper route matching.
# =============================================================================


@router.post("", response_model=FeedbackAddResponse, status_code=status.HTTP_201_CREATED)
async def add_feedback(
    request: FeedbackAddRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    feedback_manager: Any = Depends(get_feedback_manager),
) -> FeedbackAddResponse:
    """Add feedback to a trajectory."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to add feedback") from e


@router.get("/queue")
async def get_relearning_queue(
    limit: int = Query(10, ge=1, le=100, description="Maximum items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),  # noqa: ARG001
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    feedback_manager: Any = Depends(get_feedback_manager),
) -> dict[str, Any]:
    """Get trajectories marked for relearning."""
    try:
        queue = feedback_manager.get_relearning_queue(limit=limit)

        return {
            "queue": queue,
            "total": len(queue),
        }

    except Exception as e:
        logger.error(f"Relearning queue error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve relearning queue") from e


@router.post("/score", response_model=FeedbackScoreResponse)
async def calculate_score(
    request: FeedbackScoreRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    feedback_manager: Any = Depends(get_feedback_manager),
) -> FeedbackScoreResponse:
    """Calculate effective score for a trajectory."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to calculate score") from e


@router.post("/relearn")
async def mark_for_relearning(
    request: FeedbackRelearnRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    feedback_manager: Any = Depends(get_feedback_manager),
) -> dict[str, Any]:
    """Mark a trajectory for relearning."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to mark for relearning") from e


# Dynamic path must be defined LAST to avoid matching static paths
@router.get("/{trajectory_id}", response_model=TrajectoryFeedbackListResponse)
async def get_trajectory_feedback(
    trajectory_id: str,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    feedback_manager: Any = Depends(get_feedback_manager),
) -> dict[str, Any]:
    """Get all feedback for a trajectory."""
    try:
        feedbacks = feedback_manager.get_trajectory_feedback(trajectory_id)

        return {
            "trajectory_id": trajectory_id,
            "feedbacks": feedbacks,
            "total": len(feedbacks),
        }

    except Exception as e:
        logger.error(f"Feedback get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve feedback") from e
