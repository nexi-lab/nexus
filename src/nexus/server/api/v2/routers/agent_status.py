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


async def _get_agent_registry(request: Request) -> Any:
    """Get AgentRegistry from app state (optional — may be None)."""
    return getattr(request.app.state, "agent_registry", None)


async def _require_agent_registry(request: Request) -> Any:
    """Get AgentRegistry, raising 503 if unavailable."""
    pt = getattr(request.app.state, "agent_registry", None)
    if pt is None:
        raise HTTPException(status_code=503, detail="AgentRegistry not available")
    return pt


async def _get_nexus_fs(request: Request) -> Any:
    """Get NexusFS from app state (optional — may be None)."""
    return getattr(request.app.state, "nexus_fs", None)


async def _get_vfs(request: Request) -> Any:
    """Get VFS (NexusFS) from app state."""
    vfs = getattr(request.app.state, "nexus_fs", None)
    if vfs is None:
        raise HTTPException(status_code=503, detail="VFS not available")
    return vfs


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
    agent_registry: Any = Depends(_get_agent_registry),
    nexus_fs: Any = Depends(_get_nexus_fs),
) -> AgentListResponse:
    """List agents in a zone with pagination.

    Merges two sources:
    - AgentRegistry: currently running agents (in-memory)
    - Database: registered agent identities (APIKeyModel with subject_type=agent)

    Running agents show their live state; registered-but-not-running agents
    show as "registered".
    """
    # 1. Running agents from AgentRegistry (if available)
    #    Use connection_id (real agent name) as key when available,
    #    so delegation-created workers show as "researcher" not "35c2c87cb31d"
    running_map: dict[str, Any] = {}
    if agent_registry is not None:
        for a in agent_registry.list_processes(zone_id=zone_id):
            ext = a.external_info
            agent_id = ext.connection_id if ext and ext.connection_id else a.pid
            running_map[agent_id] = AgentListItem(
                agent_id=agent_id,
                owner_id=a.owner_id,
                zone_id=a.zone_id,
                name=a.name,
                state=str(a.state),
                generation=a.generation,
            )

    # 2. Registered agents from API key database (best-effort)
    #    Cross-reference with delegation_records to distinguish top-level
    #    registered agents from delegation-created workers.
    try:
        session_factory = getattr(nexus_fs, "SessionLocal", None) if nexus_fs else None
        if session_factory is not None:
            from sqlalchemy import select

            from nexus.storage.models.agents import DelegationRecordModel
            from nexus.storage.models.auth import APIKeyModel

            session = session_factory()
            try:
                # Find all delegated worker agent IDs
                delegated_ids: set[str] = set()
                try:
                    deleg_rows = session.scalars(
                        select(DelegationRecordModel.agent_id).where(
                            DelegationRecordModel.status == "active"
                        )
                    ).all()
                    delegated_ids = set(deleg_rows)
                except Exception:
                    pass

                # #3871 round 6: filter via api_key_zones junction so the
                # zone_id query parameter actually scopes the listing. Pre-#3871
                # this used APIKeyModel.zone_id, which is now NULL for keys
                # minted post-Phase 2 — that allowed cross-zone leakage where
                # an agent in 'ops' could be listed under '?zone_id=eng'.
                from nexus.storage.models.auth import APIKeyZoneModel

                rows = session.execute(
                    select(APIKeyModel, APIKeyZoneModel.zone_id)
                    .join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
                    .where(
                        APIKeyModel.subject_type == "agent",
                        APIKeyModel.revoked == 0,
                        APIKeyZoneModel.zone_id == zone_id,
                    )
                ).all()
                for key, key_zone_id in rows:
                    aid = key.subject_id
                    if aid and aid not in running_map:
                        state = "delegated" if aid in delegated_ids else "registered"
                        running_map[aid] = AgentListItem(
                            agent_id=aid,
                            owner_id=key.user_id or "",
                            zone_id=key_zone_id,
                            name=key.name or aid,
                            state=state,
                            generation=0,
                        )
            finally:
                session.close()
    except Exception:
        logger.debug("Could not fetch registered agents from database", exc_info=True)

    all_agents = sorted(running_map.values(), key=lambda a: a.agent_id)
    total = len(all_agents)
    page = all_agents[offset : offset + limit]
    return AgentListResponse(
        agents=page,
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
    agent_registry: Any = Depends(_require_agent_registry),
) -> AgentStatusResponse:
    """Get computed status for an agent."""
    _authorize_agent_access(auth_result, agent_id, "read status of")
    desc = agent_registry.get(agent_id)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    last_hb = (
        desc.external_info.last_heartbeat
        if desc.external_info and desc.external_info.last_heartbeat
        else None
    )

    return AgentStatusResponse(
        phase=str(desc.state),
        observed_generation=desc.generation,
        conditions=[],
        resource_usage=AgentResourceUsageModel(),
        last_heartbeat=last_hb.isoformat() if last_hb else None,
        last_activity=desc.updated_at.isoformat(),
        inbox_depth=0,
        context_usage_pct=0.0,
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
    agent_registry: Any = Depends(_require_agent_registry),
    vfs: Any = Depends(_get_vfs),
) -> AgentSpecResponse:
    """Set the desired state spec for an agent."""
    _authorize_agent_access(auth_result, agent_id, "set spec for")

    desc = agent_registry.get(agent_id)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    import json as _json

    from nexus.contracts.types import parse_operation_context

    spec_data = body.model_dump()
    # Increment spec_generation
    spec_path = f"/{desc.zone_id}/agents/{agent_id}/settings.json"
    existing_gen = 0
    try:
        ctx = parse_operation_context(None)
        raw = vfs.sys_read(spec_path, context=ctx)
        if raw:
            existing = _json.loads(raw if isinstance(raw, str) else raw.decode())
            existing_gen = existing.get("spec_generation", 0)
    except Exception:
        pass
    spec_data["spec_generation"] = existing_gen + 1

    ctx = parse_operation_context(None)
    vfs.write(spec_path, _json.dumps(spec_data).encode(), context=ctx)

    return AgentSpecResponse(
        agent_type=body.agent_type,
        capabilities=sorted(body.capabilities),
        resource_requests=body.resource_requests,
        resource_limits=body.resource_limits,
        qos_class=body.qos_class,
        zone_affinity=body.zone_affinity,
        spec_generation=spec_data["spec_generation"],
    )


@router.get(
    "/api/v2/agents/{agent_id}/spec",
    response_model=AgentSpecResponse,
    summary="Get agent spec (desired state)",
    description="Returns the stored desired state specification for an agent.",
)
async def get_agent_spec(
    agent_id: str,
    auth_result: dict = Depends(_get_require_auth()),
    agent_registry: Any = Depends(_require_agent_registry),
    vfs: Any = Depends(_get_vfs),
) -> AgentSpecResponse:
    """Get the stored spec for an agent."""
    _authorize_agent_access(auth_result, agent_id, "read spec of")

    desc = agent_registry.get(agent_id)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' has no spec")

    import json as _json

    from nexus.contracts.types import parse_operation_context

    spec_path = f"/{desc.zone_id}/agents/{agent_id}/settings.json"
    try:
        ctx = parse_operation_context(None)
        raw = vfs.sys_read(spec_path, context=ctx)
        data = _json.loads(raw if isinstance(raw, str) else raw.decode())
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' has no spec") from exc

    return AgentSpecResponse(
        agent_type=data.get("agent_type", "unknown"),
        capabilities=sorted(data.get("capabilities", [])),
        resource_requests=AgentResourcesModel(**data.get("resource_requests", {})),
        resource_limits=AgentResourcesModel(**data.get("resource_limits", {})),
        qos_class=data.get("qos_class", "standard"),
        zone_affinity=data.get("zone_affinity"),
        spec_generation=data.get("spec_generation", 0),
    )


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


# ---------------------------------------------------------------------------
# Agent permissions
# ---------------------------------------------------------------------------


class PermissionTuple(BaseModel):
    """Single ReBAC permission tuple."""

    relation: str
    object_type: str
    object_id: str
    zone_id: str | None = None


class AgentPermissionsResponse(BaseModel):
    """Response listing an agent's permissions."""

    agent_id: str
    permissions: list[PermissionTuple]
    total: int


@router.get(
    "/api/v2/agents/{agent_id}/permissions",
    response_model=AgentPermissionsResponse,
    summary="List agent permissions",
    description="Returns ReBAC permission tuples for the specified agent.",
)
async def get_agent_permissions(
    agent_id: str,
    _auth: dict = Depends(_get_require_auth()),
    nexus_fs: Any = Depends(_get_nexus_fs),
) -> AgentPermissionsResponse:
    """List ReBAC permission tuples for an agent."""
    tuples: list[PermissionTuple] = []
    try:
        # Use ReBACManager directly (available on NexusFS via __getattr__ → _SERVICE_ALIASES)
        session_factory = getattr(nexus_fs, "SessionLocal", None)
        if session_factory is not None:
            from sqlalchemy import select

            from nexus.storage.models.permissions import ReBACTupleModel

            session = session_factory()
            try:
                rows = session.scalars(
                    select(ReBACTupleModel).where(
                        ReBACTupleModel.subject_type == "agent",
                        ReBACTupleModel.subject_id == agent_id,
                    )
                ).all()
                for row in rows:
                    tuples.append(
                        PermissionTuple(
                            relation=row.relation,
                            object_type=row.object_type,
                            object_id=row.object_id,
                            zone_id=getattr(row, "zone_id", None),
                        )
                    )
            finally:
                session.close()
    except Exception:
        logger.debug("Could not fetch permissions for agent %s", agent_id, exc_info=True)

    return AgentPermissionsResponse(
        agent_id=agent_id,
        permissions=tuples,
        total=len(tuples),
    )
