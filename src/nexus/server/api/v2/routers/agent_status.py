"""Agent spec/status REST API endpoints (Issue #2169).

Provides:
- GET  /api/v2/agents/{agent_id}/status  - Computed agent status
- PUT  /api/v2/agents/{agent_id}/spec    - Set agent spec
- GET  /api/v2/agents/{agent_id}/spec    - Get agent spec

All endpoints are authenticated via existing auth middleware.

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from nexus.server.api.v2.dependencies import _get_require_auth

logger = logging.getLogger(__name__)

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
    _auth: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentStatusResponse:
    """Get computed status for an agent."""
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
    _auth: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentSpecResponse:
    """Set the desired state spec for an agent."""
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


@router.get(
    "/api/v2/agents/{agent_id}/spec",
    response_model=AgentSpecResponse,
    summary="Get agent spec (desired state)",
    description="Returns the stored desired state specification for an agent.",
)
async def get_agent_spec(
    agent_id: str,
    _auth: dict = Depends(_get_require_auth()),
    registry: Any = Depends(_get_async_agent_registry),
) -> AgentSpecResponse:
    """Get the stored spec for an agent."""
    stored = await registry.get_spec(agent_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' has no spec")

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
