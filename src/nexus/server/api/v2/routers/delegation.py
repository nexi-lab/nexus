"""Agent Delegation API endpoints (Issue #1271).

Provides 3 endpoints for coordinator-initiated agent delegation:
- POST   /api/v2/agents/delegate              — Delegate agent identity
- DELETE /api/v2/agents/delegate/{id}          — Revoke delegation
- GET    /api/v2/agents/delegate               — List coordinator's delegations
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nexus.delegation.service import MAX_TTL_SECONDS as _MAX_TTL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/agents/delegate", tags=["delegation"])

# =============================================================================
# Lazy imports (avoid circular imports with fastapi_server)
# =============================================================================


def _get_require_auth() -> Any:
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_delegation_service() -> Any:
    """Lazily construct DelegationService from app state."""
    from nexus.server.fastapi_server import _fastapi_app

    if _fastapi_app is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    state = _fastapi_app.state
    cached = getattr(state, "_delegation_service", None)
    if cached is not None:
        return cached

    # Construct from available components
    session_factory = getattr(state, "session_factory", None) or getattr(
        getattr(state, "nexus_fs", None), "SessionLocal", None
    )
    if session_factory is None:
        raise HTTPException(status_code=503, detail="Session factory not available")

    rebac_manager = getattr(state, "rebac_manager", None) or getattr(
        getattr(state, "nexus_fs", None), "_rebac_manager", None
    )
    if rebac_manager is None:
        raise HTTPException(status_code=503, detail="ReBAC manager not available")

    namespace_manager = getattr(state, "namespace_manager", None) or getattr(
        getattr(state, "nexus_fs", None), "_namespace_manager", None
    )
    entity_registry = getattr(state, "entity_registry", None) or getattr(
        getattr(state, "nexus_fs", None), "_entity_registry", None
    )
    agent_registry = getattr(state, "agent_registry", None) or getattr(
        getattr(state, "nexus_fs", None), "_agent_registry", None
    )

    from nexus.delegation.service import DelegationService

    service = DelegationService(
        session_factory=session_factory,
        rebac_manager=rebac_manager,
        namespace_manager=namespace_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )
    state._delegation_service = service
    return service


# =============================================================================
# Pydantic models
# =============================================================================


class DelegateRequest(BaseModel):
    """Request to create a delegated worker agent."""

    worker_id: str = Field(..., description="Unique ID for the worker agent")
    worker_name: str = Field(..., description="Human-readable name for the worker")
    namespace_mode: str = Field(..., description="Delegation mode: 'copy', 'clean', or 'shared'")
    remove_grants: list[str] | None = Field(
        default=None, description="Paths to exclude (copy mode)"
    )
    add_grants: list[str] | None = Field(
        default=None, description="Paths to include (clean mode, must be subset of parent)"
    )
    readonly_paths: list[str] | None = Field(
        default=None, description="Paths to downgrade to read-only (copy mode)"
    )
    scope_prefix: str | None = Field(default=None, description="Path prefix filter for grants")
    ttl_seconds: int | None = Field(
        default=None,
        gt=0,
        le=_MAX_TTL,
        description="Delegation TTL in seconds (max 86400 = 24h)",
    )


class DelegateResponse(BaseModel):
    """Response after creating a delegation."""

    delegation_id: str
    worker_agent_id: str
    api_key: str
    mount_table: list[str]
    expires_at: datetime | None
    delegation_mode: str


class DelegationListItem(BaseModel):
    """Summary of a delegation for list responses."""

    delegation_id: str
    agent_id: str
    parent_agent_id: str
    delegation_mode: str
    scope_prefix: str | None
    lease_expires_at: datetime | None
    zone_id: str | None
    created_at: datetime


class DelegationListResponse(BaseModel):
    """Response for listing delegations."""

    delegations: list[DelegationListItem]
    count: int


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=DelegateResponse)
async def create_delegation(
    request: DelegateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> DelegateResponse:
    """Create a delegated worker agent with narrowed permissions.

    The caller must be an agent (not a user, not a delegated agent).
    """
    # Validate caller is an agent
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can delegate. Caller subject_type must be 'agent'.",
        )

    coordinator_agent_id = auth_result.get("subject_id", "")
    coordinator_owner_id = auth_result.get("user_id") or auth_result.get("metadata", {}).get(
        "user_id", ""
    )
    zone_id = auth_result.get("zone_id")

    # Validate namespace_mode
    from nexus.delegation.models import DelegationMode

    try:
        mode = DelegationMode(request.namespace_mode)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid namespace_mode: {request.namespace_mode!r}. "
            "Must be 'copy', 'clean', or 'shared'.",
        ) from e

    service = _get_delegation_service()

    try:
        result = service.delegate(
            coordinator_agent_id=coordinator_agent_id,
            coordinator_owner_id=coordinator_owner_id,
            worker_id=request.worker_id,
            worker_name=request.worker_name,
            delegation_mode=mode,
            zone_id=zone_id,
            scope_prefix=request.scope_prefix,
            remove_grants=request.remove_grants,
            add_grants=request.add_grants,
            readonly_paths=request.readonly_paths,
            ttl_seconds=request.ttl_seconds,
        )
    except Exception as e:
        _handle_delegation_error(e)
        raise  # unreachable, but satisfies type checker

    return DelegateResponse(
        delegation_id=result.delegation_id,
        worker_agent_id=result.worker_agent_id,
        api_key=result.api_key,
        mount_table=result.mount_table,
        expires_at=result.expires_at,
        delegation_mode=result.delegation_mode.value,
    )


@router.delete("/{delegation_id}")
async def revoke_delegation(
    delegation_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Revoke a delegation, removing all worker grants and API keys."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can revoke delegations.",
        )

    service = _get_delegation_service()

    try:
        service.revoke_delegation(delegation_id)
    except Exception as e:
        _handle_delegation_error(e)
        raise  # unreachable, but satisfies type checker

    return {"status": "revoked", "delegation_id": delegation_id}


@router.get("", response_model=DelegationListResponse)
async def list_delegations(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> DelegationListResponse:
    """List all delegations created by the calling agent."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can list delegations.",
        )

    coordinator_agent_id = auth_result.get("subject_id", "")
    service = _get_delegation_service()

    records = service.list_delegations(coordinator_agent_id)
    items = [
        DelegationListItem(
            delegation_id=r.delegation_id,
            agent_id=r.agent_id,
            parent_agent_id=r.parent_agent_id,
            delegation_mode=r.delegation_mode.value,
            scope_prefix=r.scope_prefix,
            lease_expires_at=r.lease_expires_at,
            zone_id=r.zone_id,
            created_at=r.created_at,
        )
        for r in records
    ]

    return DelegationListResponse(delegations=items, count=len(items))


# =============================================================================
# Error handling
# =============================================================================


def _handle_delegation_error(e: Exception) -> None:
    """Map domain errors to HTTP responses. Always raises."""
    from nexus.delegation.errors import (
        DelegationChainError,
        DelegationNotFoundError,
        EscalationError,
        TooManyGrantsError,
    )

    if isinstance(e, EscalationError):
        raise HTTPException(status_code=403, detail=str(e)) from e
    if isinstance(e, TooManyGrantsError):
        raise HTTPException(status_code=400, detail=str(e)) from e
    if isinstance(e, DelegationChainError):
        raise HTTPException(status_code=403, detail=str(e)) from e
    if isinstance(e, DelegationNotFoundError):
        raise HTTPException(status_code=404, detail=str(e)) from e
    if isinstance(e, HTTPException):
        raise
    logger.error("[Delegation] Unexpected error: %s", e, exc_info=True)
    raise HTTPException(status_code=500, detail=f"Delegation failed: {e}") from e
