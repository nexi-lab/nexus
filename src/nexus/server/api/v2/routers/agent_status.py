"""Agent spec/status/warmup REST API endpoints (Issues #2169, #2172).

Provides:
- GET  /api/v2/agents                    - List agents in zone
- GET  /api/v2/agents/{agent_id}/status  - Computed agent status
- PUT  /api/v2/agents/{agent_id}/spec    - Set agent spec
- GET  /api/v2/agents/{agent_id}/spec    - Get agent spec
- POST /api/v2/agents/{agent_id}/warmup  - Trigger agent warmup

All endpoints are authenticated via existing auth middleware.

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from nexus.server.api.v2.dependencies import _get_require_auth
from nexus.server.dependencies import get_operation_context

logger = logging.getLogger(__name__)


def _authorize_agent_access(auth_result: dict, agent_id: str, action: str = "access") -> None:
    """Verify caller is authorized to act on this agent."""
    ctx = get_operation_context(auth_result)
    if ctx.is_admin:
        return
    if ctx.subject_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"Not authorized to {action} agent '{agent_id}'",
        )


router = APIRouter(tags=["agents"])

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AgentResourcesModel(BaseModel):
    """Resource requests or limits."""

    token_budget: int | None = None
    token_request: int | None = None
    storage_limit_mb: int | None = None
    context_limit: int | None = None


class AgentResourceUsageModel(BaseModel):
    """Observed resource consumption."""

    tokens_used: int = 0
    storage_used_mb: float = 0.0
    context_usage_pct: float = 0.0


class AgentConditionModel(BaseModel):
    """A single condition describing agent health."""

    type: str
    status: str
    reason: str
    message: str
    last_transition: str
    observed_generation: int


class AgentSpecRequest(BaseModel):
    """Input body for PUT /spec."""

    agent_type: str
    capabilities: list[str] = []
    resource_requests: AgentResourcesModel = AgentResourcesModel()
    resource_limits: AgentResourcesModel = AgentResourcesModel()
    qos_class: str = "standard"
    zone_affinity: str | None = None


class AgentSpecResponse(BaseModel):
    """Response body for spec endpoints."""

    agent_type: str
    capabilities: list[str]
    resource_requests: AgentResourcesModel
    resource_limits: AgentResourcesModel
    qos_class: str
    zone_affinity: str | None
    spec_generation: int


class AgentStatusResponse(BaseModel):
    """Response body for status endpoint."""

    phase: str
    observed_generation: int
    conditions: list[AgentConditionModel]
    resource_usage: AgentResourceUsageModel
    last_heartbeat: str | None
    last_activity: str | None
    inbox_depth: int
    context_usage_pct: float


class WarmupStepModel(BaseModel):
    """A single warmup step in the request body (Issue #2172)."""

    name: str
    timeout_seconds: float = 30.0
    required: bool = True


class WarmupRequest(BaseModel):
    """Request body for POST /warmup (Issue #2172)."""

    steps: list[WarmupStepModel] | None = None


class WarmupResponse(BaseModel):
    """Response body for POST /warmup (Issue #2172)."""

    success: bool
    agent_id: str
    steps_completed: list[str] = []
    steps_skipped: list[str] = []
    failed_step: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


class AgentListItem(BaseModel):
    """Summary of an agent for list responses."""

    agent_id: str
    owner_id: str
    zone_id: str | None
    name: str | None
    state: str
    generation: int


class AgentListResponse(BaseModel):
    """Paginated agent list response."""

    agents: list[AgentListItem]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Helpers (DRY fix — Issue #2172)
# ---------------------------------------------------------------------------


def _spec_to_response(stored: Any) -> AgentSpecResponse:
    """Convert an AgentSpec domain object to AgentSpecResponse.

    Eliminates duplication across set_agent_spec and get_agent_spec endpoints.
    """
    return AgentSpecResponse(
        agent_type=stored.agent_type,
        capabilities=sorted(stored.capabilities),
        resource_requests=AgentResourcesModel(
            token_budget=stored.resource_requests.token_budget,
            token_request=stored.resource_requests.token_request,
            storage_limit_mb=stored.resource_requests.storage_limit_mb,
            context_limit=stored.resource_requests.context_limit,
        ),
        resource_limits=AgentResourcesModel(
            token_budget=stored.resource_limits.token_budget,
            token_request=stored.resource_limits.token_request,
            storage_limit_mb=stored.resource_limits.storage_limit_mb,
            context_limit=stored.resource_limits.context_limit,
        ),
        qos_class=str(stored.qos_class),
        zone_affinity=stored.zone_affinity,
        spec_generation=stored.spec_generation,
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def _get_async_agent_registry(request: Request) -> Any:
    """Get AsyncAgentRegistry from app state."""
    registry = getattr(request.app.state, "async_agent_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Agent registry not available")
    return registry


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/v2/agents",
    response_model=AgentListResponse,
    summary="List agents in zone",
    description="Returns a paginated list of agents registered in the specified zone.",
)
async def list_agents(
    zone_id: str = Query(default="root", description="Zone to list agents from"),
    limit: int = Query(default=50, ge=1, le=200, description="Max agents to return"),
    offset: int = Query(default=0, ge=0, description="Agents to skip"),
    _auth: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentListResponse:
    """List agents in a zone with pagination."""
    all_agents = await registry.list_by_zone(zone_id)
    total = len(all_agents)
    page = all_agents[offset : offset + limit]
    return AgentListResponse(
        agents=[
            AgentListItem(
                agent_id=a.agent_id,
                owner_id=a.owner_id,
                zone_id=a.zone_id,
                name=a.name,
                state=a.state,
                generation=a.generation,
            )
            for a in page
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v2/agents/{agent_id}/status",
    response_model=AgentStatusResponse,
    summary="Get computed agent status",
    description=(
        "Returns the current observed status for an agent, computed from "
        "its lifecycle state, heartbeat data, and stored spec. "
        "Includes drift detection via generation counters."
    ),
)
async def get_agent_status(
    agent_id: str,
    auth_result: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentStatusResponse:
    """Get computed status for an agent."""
    _authorize_agent_access(auth_result, agent_id, "read status of")
    status = await registry.get_status(agent_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    return AgentStatusResponse(
        phase=str(status.phase),
        observed_generation=status.observed_generation,
        conditions=[
            AgentConditionModel(
                type=c.type,
                status=c.status,
                reason=c.reason,
                message=c.message,
                last_transition=c.last_transition.isoformat(),
                observed_generation=c.observed_generation,
            )
            for c in status.conditions
        ],
        resource_usage=AgentResourceUsageModel(
            tokens_used=status.resource_usage.tokens_used,
            storage_used_mb=status.resource_usage.storage_used_mb,
            context_usage_pct=status.resource_usage.context_usage_pct,
        ),
        last_heartbeat=status.last_heartbeat.isoformat() if status.last_heartbeat else None,
        last_activity=status.last_activity.isoformat() if status.last_activity else None,
        inbox_depth=status.inbox_depth,
        context_usage_pct=status.context_usage_pct,
    )


@router.put(
    "/api/v2/agents/{agent_id}/spec",
    response_model=AgentSpecResponse,
    summary="Set agent spec (desired state)",
    description=(
        "Stores the desired state specification for an agent. "
        "Increments the spec_generation counter for drift detection."
    ),
)
async def set_agent_spec(
    agent_id: str,
    body: AgentSpecRequest,
    auth_result: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentSpecResponse:
    """Set the desired state spec for an agent."""
    _authorize_agent_access(auth_result, agent_id, "set spec for")
    from nexus.contracts.agent_types import AgentResources, AgentSpec, QoSClass

    try:
        qos = QoSClass(body.qos_class)
    except ValueError:
        valid = [e.value for e in QoSClass]
        raise HTTPException(
            status_code=422,
            detail=f"Invalid qos_class {body.qos_class!r}. Valid: {', '.join(valid)}",
        ) from None

    spec = AgentSpec(
        agent_type=body.agent_type,
        capabilities=frozenset(body.capabilities),
        resource_requests=AgentResources(
            token_budget=body.resource_requests.token_budget,
            token_request=body.resource_requests.token_request,
            storage_limit_mb=body.resource_requests.storage_limit_mb,
            context_limit=body.resource_requests.context_limit,
        ),
        resource_limits=AgentResources(
            token_budget=body.resource_limits.token_budget,
            token_request=body.resource_limits.token_request,
            storage_limit_mb=body.resource_limits.storage_limit_mb,
            context_limit=body.resource_limits.context_limit,
        ),
        qos_class=qos,
        zone_affinity=body.zone_affinity,
    )

    try:
        stored = await registry.set_spec(agent_id, spec)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _spec_to_response(stored)


@router.get(
    "/api/v2/agents/{agent_id}/spec",
    response_model=AgentSpecResponse,
    summary="Get agent spec (desired state)",
    description="Returns the stored desired state specification for an agent.",
)
async def get_agent_spec(
    agent_id: str,
    auth_result: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentSpecResponse:
    """Get the stored spec for an agent."""
    _authorize_agent_access(auth_result, agent_id, "read spec of")
    stored = await registry.get_spec(agent_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' has no spec")

    return _spec_to_response(stored)


# ---------------------------------------------------------------------------
# Warmup endpoint (Issue #2172)
# ---------------------------------------------------------------------------


async def _get_warmup_service(request: Request) -> Any:
    """Get AgentWarmupService from app state."""
    service = getattr(request.app.state, "agent_warmup_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Agent warmup service not available")
    return service


@router.post(
    "/api/v2/agents/{agent_id}/warmup",
    response_model=WarmupResponse,
    summary="Trigger agent warmup (Issue #2172)",
    description=(
        "Execute structured warmup steps for an agent before it accepts work. "
        "Steps run sequentially with per-step timeouts. Required steps must "
        "pass for the agent to transition from UNKNOWN to CONNECTED."
    ),
)
async def warmup_agent(
    agent_id: str,
    body: WarmupRequest | None = None,
    auth_result: dict = Depends(_get_require_auth()),
    warmup_service: Any = Depends(_get_warmup_service),
) -> WarmupResponse:
    """Trigger agent warmup."""
    _authorize_agent_access(auth_result, agent_id, "warm up")
    from datetime import timedelta

    from nexus.contracts.agent_warmup_types import WarmupStep

    # Convert request body steps to domain objects (or use defaults)
    steps: list[WarmupStep] | None = None
    if body is not None and body.steps is not None:
        steps = [
            WarmupStep(
                name=s.name,
                timeout=timedelta(seconds=s.timeout_seconds),
                required=s.required,
            )
            for s in body.steps
        ]

    result = await warmup_service.warmup(agent_id, steps=steps)

    return WarmupResponse(
        success=result.success,
        agent_id=result.agent_id,
        steps_completed=list(result.steps_completed),
        steps_skipped=list(result.steps_skipped),
        failed_step=result.failed_step,
        error=result.error,
        duration_ms=result.duration_ms,
    )
