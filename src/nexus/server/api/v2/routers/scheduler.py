"""Scheduler REST API endpoints.

Provides endpoints for task scheduling with hybrid priority:
- POST   /api/v2/scheduler/submit              - Submit a task
- GET    /api/v2/scheduler/task/{id}            - Get task status
- POST   /api/v2/scheduler/task/{id}/cancel     - Cancel a task
- GET    /api/v2/scheduler/metrics              - Queue metrics (Issue #1274)
- POST   /api/v2/scheduler/classify             - Classify request (Issue #1274)

Related: Issue #1212, #1274
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from nexus.system_services.scheduler.constants import TIER_ALIASES, RequestState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/scheduler", tags=["scheduler"])

VALID_PRIORITIES = frozenset(TIER_ALIASES.keys())
VALID_REQUEST_STATES = frozenset(s.value for s in RequestState)

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
    # Astraea extensions (Issue #1274)
    request_state: str = Field(
        default="pending",
        description="Request state: io_wait, compute, tool_call, idle, pending",
    )
    estimated_service_time: float = Field(
        default=30.0, gt=0, description="Estimated execution time in seconds"
    )

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        if v not in VALID_PRIORITIES:
            valid = ", ".join(sorted(VALID_PRIORITIES))
            raise ValueError(f"Invalid priority '{v}'. Must be one of: {valid}")
        return v

    @field_validator("request_state")
    @classmethod
    def validate_request_state(cls, v: str) -> str:
        if v not in VALID_REQUEST_STATES:
            valid = ", ".join(sorted(VALID_REQUEST_STATES))
            raise ValueError(f"Invalid request_state '{v}'. Must be one of: {valid}")
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
    # Astraea extensions (Issue #1274)
    priority_class: str = "batch"
    request_state: str = "pending"


class CancelResponse(BaseModel):
    """Cancel operation result."""

    cancelled: bool
    task_id: str


class ClassifyRequest(BaseModel):
    """Request to classify a task into a priority class."""

    priority: str = Field(default="normal", description="Priority tier")
    request_state: str = Field(default="pending", description="Request state")

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        if v not in VALID_PRIORITIES:
            valid = ", ".join(sorted(VALID_PRIORITIES))
            raise ValueError(f"Invalid priority '{v}'. Must be one of: {valid}")
        return v


class ClassifyResponse(BaseModel):
    """Classification result."""

    priority_class: str


class MetricsResponse(BaseModel):
    """Scheduler metrics."""

    queue_by_class: list[dict[str, Any]]
    fair_share: dict[str, Any]
    use_hrrn: bool


# =============================================================================
# Dependencies
# =============================================================================


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.dependencies import require_auth

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
    from nexus.contracts.protocols.scheduler import AgentRequest

    agent_id = _extract_agent_id(auth_result)
    tier = TIER_ALIASES[request.priority]
    agent_request = AgentRequest(
        agent_id=agent_id,
        zone_id=None,
        priority=tier.value,
        executor_id=request.executor,
        task_type=request.task_type,
        payload=request.payload,
        request_state=request.request_state,
        deadline=request.deadline.isoformat() if request.deadline else None,
        boost_amount=str(request.boost),
        estimated_service_time=request.estimated_service_time,
        idempotency_key=request.idempotency_key,
    )

    task_id = await scheduler.submit(agent_request)
    status = await scheduler.get_status(task_id)
    if status is None:
        raise HTTPException(status_code=500, detail="Task creation failed")
    return TaskStatusResponse(**status)


@router.get("/task/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    scheduler: Any = Depends(get_scheduler_service),
) -> TaskStatusResponse:
    """Get task status by ID (owner-scoped)."""
    agent_id = _extract_agent_id(auth_result)
    status = await scheduler.get_status_scoped(task_id, agent_id=agent_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatusResponse(**status)


@router.post("/task/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(
    task_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    scheduler: Any = Depends(get_scheduler_service),
) -> CancelResponse:
    """Cancel a queued task (owner-scoped).

    Only tasks with status 'queued' that belong to the caller can be cancelled.
    Returns cancelled=true if successful, false otherwise.
    """
    agent_id = _extract_agent_id(auth_result)
    cancelled = await scheduler.cancel_by_id_scoped(task_id, agent_id=agent_id)
    return CancelResponse(cancelled=cancelled, task_id=task_id)


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    scheduler: Any = Depends(get_scheduler_service),
) -> MetricsResponse:
    """Get scheduler queue metrics and fair-share snapshots."""
    data = await scheduler.metrics()
    return MetricsResponse(**data)


@router.post("/classify", response_model=ClassifyResponse)
async def classify_request_endpoint(
    request: ClassifyRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    scheduler: Any = Depends(get_scheduler_service),
) -> ClassifyResponse:
    """Classify a request into a priority class."""
    from nexus.contracts.protocols.scheduler import AgentRequest

    agent_request = AgentRequest(
        agent_id="",
        zone_id=None,
        priority=TIER_ALIASES[request.priority].value,
        request_state=request.request_state,
    )
    priority_class = await scheduler.classify(agent_request)
    return ClassifyResponse(priority_class=priority_class)
