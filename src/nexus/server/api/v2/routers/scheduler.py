"""Scheduler REST API endpoints.

Provides endpoints for task scheduling with hybrid priority:
- POST   /api/v2/scheduler/submit              - Submit a task
- GET    /api/v2/scheduler/task/{id}            - Get task status
- POST   /api/v2/scheduler/task/{id}/cancel     - Cancel a task

Related: Issue #1212
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from nexus.scheduler.constants import TIER_ALIASES, PriorityTier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/scheduler", tags=["scheduler"])

VALID_PRIORITIES = frozenset(TIER_ALIASES.keys())


# =============================================================================
# Pydantic Models
# =============================================================================


class SubmitTaskRequest(BaseModel):
    """Request to submit a task for scheduling."""

    executor: str = Field(..., min_length=1, description="Target agent/service to execute")
    task_type: str = Field(..., min_length=1, description="Task type identifier")
    payload: dict[str, Any] = Field(default_factory=dict, description="Task data")
    priority: str = Field(
        default="normal", description="Priority: critical, high, normal, low, best_effort"
    )
    deadline: datetime | None = Field(default=None, description="Optional deadline (ISO 8601)")
    boost: str = Field(default="0", description="Credits for priority boost (decimal string)")
    idempotency_key: str | None = Field(default=None, description="Deduplication key")

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        if v not in VALID_PRIORITIES:
            valid = ", ".join(sorted(VALID_PRIORITIES))
            raise ValueError(f"Invalid priority '{v}'. Must be one of: {valid}")
        return v


class TaskStatusResponse(BaseModel):
    """Task status information."""

    id: str
    status: str
    agent_id: str
    executor_id: str
    task_type: str
    priority_tier: str
    effective_tier: int
    enqueued_at: str
    started_at: str | None = None
    completed_at: str | None = None
    deadline: str | None = None
    boost_amount: str = "0"
    error_message: str | None = None


class CancelResponse(BaseModel):
    """Cancel operation result."""

    cancelled: bool
    task_id: str


# =============================================================================
# Dependencies
# =============================================================================


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def get_scheduler_service(request: Request) -> Any:
    """Get SchedulerService from app state."""
    service = getattr(request.app.state, "scheduler_service", None)
    if not service:
        raise HTTPException(status_code=503, detail="Scheduler service not available")
    return service


def _extract_agent_id(auth_result: dict[str, Any]) -> str:
    """Extract agent_id from auth result (x_agent_id header > subject_id)."""
    x_agent_id = auth_result.get("x_agent_id")
    if x_agent_id:
        return str(x_agent_id)
    return str(auth_result.get("subject_id", "anonymous"))


# =============================================================================
# Response Converters
# =============================================================================


def _task_to_response(task: Any) -> TaskStatusResponse:
    """Convert ScheduledTask to API response."""
    return TaskStatusResponse(
        id=task.id,
        status=task.status,
        agent_id=task.agent_id,
        executor_id=task.executor_id,
        task_type=task.task_type,
        priority_tier=PriorityTier(task.priority_tier).name.lower(),
        effective_tier=task.effective_tier,
        enqueued_at=task.enqueued_at.isoformat() if task.enqueued_at else "",
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        deadline=task.deadline.isoformat() if task.deadline else None,
        boost_amount=str(task.boost_amount),
        error_message=task.error_message,
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/submit", response_model=TaskStatusResponse, status_code=201)
async def submit_task(
    request: SubmitTaskRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    scheduler: Any = Depends(get_scheduler_service),
) -> TaskStatusResponse:
    """Submit a task for priority scheduling.

    The task is enqueued with the specified priority and boost.
    Returns the task details with computed effective priority.
    """
    from nexus.scheduler.models import TaskSubmission

    agent_id = _extract_agent_id(auth_result)
    tier = TIER_ALIASES[request.priority]
    submission = TaskSubmission(
        agent_id=agent_id,
        executor_id=request.executor,
        task_type=request.task_type,
        payload=request.payload,
        priority=tier,
        deadline=request.deadline,
        boost_amount=Decimal(request.boost),
        idempotency_key=request.idempotency_key,
    )

    task = await scheduler.submit_task(submission)
    return _task_to_response(task)


@router.get("/task/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    scheduler: Any = Depends(get_scheduler_service),
) -> TaskStatusResponse:
    """Get task status by ID."""
    task = await scheduler.get_status(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task)


@router.post("/task/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(
    task_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    scheduler: Any = Depends(get_scheduler_service),
) -> CancelResponse:
    """Cancel a queued task.

    Only tasks with status 'queued' can be cancelled.
    Returns cancelled=true if successful, false otherwise.
    """
    agent_id = _extract_agent_id(auth_result)
    cancelled = await scheduler.cancel_task(task_id, agent_id=agent_id)
    return CancelResponse(cancelled=cancelled, task_id=task_id)
