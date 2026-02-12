"""Trajectory request/response models for API v2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class TrajectoryStartRequest(ApiModel):
    """Request for POST /api/v2/trajectories."""

    task_description: str = Field(..., description="Description of the task")
    task_type: str | None = Field(None, description="Type of task")
    parent_trajectory_id: str | None = Field(None, description="Parent trajectory for nesting")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")
    path: str | None = Field(None, description="File path context")


class TrajectoryStepRequest(ApiModel):
    """Request for POST /api/v2/trajectories/{id}/steps."""

    step_type: Literal["action", "decision", "observation", "tool_call", "error"] = Field(
        ..., description="Type of step"
    )
    description: str = Field(..., description="Step description")
    result: Any | None = Field(None, description="Step result/output")
    metadata: dict[str, Any] | None = Field(None, description="Step metadata")


class TrajectoryCompleteRequest(ApiModel):
    """Request for POST /api/v2/trajectories/{id}/complete."""

    status: Literal["success", "failure", "partial", "cancelled"] = Field(
        ..., description="Completion status"
    )
    success_score: float | None = Field(None, ge=0.0, le=1.0, description="Success score")
    error_message: str | None = Field(None, description="Error message if failed")
    metrics: dict[str, Any] | None = Field(None, description="Completion metrics")


class TrajectoryQueryParams(ApiModel):
    """Query parameters for GET /api/v2/trajectories."""

    agent_id: str | None = Field(None, description="Filter by agent ID")
    task_type: str | None = Field(None, description="Filter by task type")
    status: str | None = Field(None, description="Filter by status")
    limit: int = Field(50, ge=1, le=100, description="Maximum results")
    offset: int = Field(0, ge=0, description="Number of results to skip (for pagination)")
    path: str | None = Field(None, description="Filter by path")


class TrajectoryResponse(ApiModel):
    """Response model for trajectory objects."""

    trajectory_id: str
    task_description: str
    task_type: str | None = None
    status: str
    success_score: float | None = None
    duration_ms: int | None = None
    step_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    trace: list[dict[str, Any]] | None = None


class TrajectoryGetResponse(ApiModel):
    """Response for GET /api/v2/trajectories/{id}."""

    trajectory: dict[str, Any]


class TrajectoryStartResponse(ApiModel):
    """Response for POST /api/v2/trajectories."""

    trajectory_id: str
    status: str = "in_progress"
