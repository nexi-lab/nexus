"""Feedback request/response models for API v2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class FeedbackAddRequest(ApiModel):
    """Request for POST /api/v2/feedback."""

    trajectory_id: str = Field(..., description="Trajectory to add feedback to")
    feedback_type: Literal["human", "monitoring", "ab_test", "production"] = Field(
        ..., description="Type of feedback"
    )
    score: float | None = Field(None, ge=0.0, le=1.0, description="Feedback score")
    source: str | None = Field(None, description="Feedback source identifier")
    message: str | None = Field(None, description="Feedback message")
    metrics: dict[str, Any] | None = Field(None, description="Feedback metrics")


class FeedbackScoreRequest(ApiModel):
    """Request for POST /api/v2/feedback/score."""

    trajectory_id: str = Field(..., description="Trajectory to score")
    strategy: Literal["latest", "average", "weighted"] = Field(
        "latest", description="Scoring strategy"
    )


class FeedbackRelearnRequest(ApiModel):
    """Request for POST /api/v2/feedback/relearn."""

    trajectory_id: str = Field(..., description="Trajectory to mark for relearning")
    reason: str = Field(..., description="Reason for relearning")
    priority: int = Field(5, ge=1, le=10, description="Relearning priority")


class FeedbackResponse(ApiModel):
    """Response model for feedback objects."""

    feedback_id: str
    trajectory_id: str
    feedback_type: str
    score: float | None = None
    source: str | None = None
    message: str | None = None
    created_at: str | None = None


class TrajectoryFeedbackListResponse(ApiModel):
    """Response for GET /api/v2/feedback/{trajectory_id}."""

    trajectory_id: str
    feedbacks: list[dict[str, Any]]
    total: int


class FeedbackAddResponse(ApiModel):
    """Response for POST /api/v2/feedback."""

    feedback_id: str
    status: str = "created"


class FeedbackScoreResponse(ApiModel):
    """Response for POST /api/v2/feedback/score."""

    trajectory_id: str
    effective_score: float
    strategy: str
