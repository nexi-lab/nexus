"""Top-level agent registration API endpoint (Issue #3130).

Provides:
- POST /api/v2/agents/register — Admin-only top-level agent registration.

This endpoint orchestrates identity creation, API key provisioning,
ReBAC grants, IPC directory setup, and optional Ed25519 public key
registration in a single call.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.bricks.delegation.grant_helpers import (
    MAX_REGISTRATION_GRANTS,
    GrantInput,
)
from nexus.server.dependencies import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v2/agents",
    tags=["agents"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RegisterAgentRequest(BaseModel):
    """Request body for top-level agent registration."""

    agent_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Unique agent identifier",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable agent name",
    )
    grants: list[GrantInput] = Field(
        default_factory=list,
        max_length=MAX_REGISTRATION_GRANTS,
        description="Path+role grants for the agent",
    )
    ipc: bool = Field(
        default=True,
        description="Whether to provision IPC directories (inbox/outbox/processed/dead_letter)",
    )
    public_key: str | None = Field(
        default=None,
        description="Optional hex-encoded Ed25519 public key (32 bytes = 64 hex chars). "
        "If provided, the agent can use POST /agents/{id}/verify for identity proofs. "
        "The private key must remain with the agent (never sent to the server).",
    )


class GrantResponse(BaseModel):
    """A single grant in the registration response."""

    path: str
    role: str


class RegisterAgentResponse(BaseModel):
    """Response after successful agent registration."""

    agent_id: str
    api_key: str
    key_id: str
    owner_id: str
    zone_id: str
    grants: list[GrantResponse]
    ipc_inbox: str | None
    ipc_provisioned: bool
    public_key_registered: bool


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_registration_service(request: Request) -> Any:
    """Get or create AgentRegistrationService from app state.

    Lazily constructs the service from app.state components, caching
    the result for subsequent requests.
    """
    cached = getattr(request.app.state, "_agent_registration_service", None)
    if cached is not None:
        return cached

    record_store = getattr(request.app.state, "record_store", None)
    if record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not available")

    from nexus.system_services.agents.agent_registration import AgentRegistrationService

    service = AgentRegistrationService(
        record_store=record_store,
        entity_registry=getattr(request.app.state, "entity_registry", None),
        process_table=getattr(request.app.state, "process_table", None),
        rebac_manager=getattr(request.app.state, "rebac_manager", None),
        ipc_provisioner=getattr(request.app.state, "ipc_provisioner", None),
        key_service=getattr(request.app.state, "key_service", None),
    )
    request.app.state._agent_registration_service = service
    return service


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/register", response_model=RegisterAgentResponse, status_code=201)
async def register_agent(
    body: RegisterAgentRequest,
    auth_result: dict[str, Any] = Depends(require_admin),
    service: Any = Depends(_get_registration_service),
) -> RegisterAgentResponse:
    """Register a top-level agent (admin-only).

    Creates a permanent agent identity with:
    - Registered identity in entity_registry (persistent, DB)
    - Registered in ProcessTable (runtime liveness, in-memory)
    - Permanent API key (no TTL, ``subject_type: "agent"``)
    - ReBAC permission tuples for the specified grants
    - IPC directories (inbox/outbox/processed/dead_letter) if ``ipc=True``
    - Ed25519 public key for identity verification if ``public_key`` provided

    Use ``POST /agents/{id}/warmup`` to warm up the agent after registration.
    """
    from nexus.system_services.agents.agent_registration import AgentAlreadyExistsError

    owner_id = auth_result.get("user_id") or auth_result.get("subject_id", "")
    zone_id = auth_result.get("zone_id")

    # Validate public_key format before passing to service
    if body.public_key is not None:
        try:
            key_bytes = bytes.fromhex(body.public_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid public_key: must be hex-encoded. {exc}",
            ) from exc
        if len(key_bytes) != 32:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid public_key: Ed25519 public key must be exactly 32 bytes "
                f"(64 hex chars), got {len(key_bytes)} bytes.",
            )

    try:
        result = await service.register(
            agent_id=body.agent_id,
            name=body.name,
            owner_id=owner_id,
            zone_id=zone_id,
            grants=body.grants if body.grants else None,
            ipc=body.ipc,
            public_key_hex=body.public_key,
        )
    except AgentAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "[AgentRegistration] Unexpected error registering agent %s: %s",
            body.agent_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Agent registration failed: {exc}") from exc

    return RegisterAgentResponse(
        agent_id=result.agent_id,
        api_key=result.api_key,
        key_id=result.key_id,
        owner_id=result.owner_id,
        zone_id=result.zone_id,
        grants=[GrantResponse(path=g.path, role=g.role) for g in (body.grants or [])],
        ipc_inbox=result.ipc_inbox,
        ipc_provisioned=result.ipc_provisioned,
        public_key_registered=result.public_key_registered,
    )
