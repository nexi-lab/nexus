"""Agent Identity REST API endpoints (KYA Phase 1, Issue #1355).

Provides cryptographic identity operations:
- POST /agents/{agent_id}/verify — Identity verification (any authenticated user)
- GET  /agents/{agent_id}/keys   — List agent's public keys (any authenticated user)
- POST /agents/{agent_id}/keys/rotate — Generate a new key pair (owner/admin only)
- DELETE /agents/{agent_id}/keys/{key_id} — Revoke a key (owner/admin only)

All endpoints are authenticated via existing auth middleware.
All endpoints use sync ``def`` for threadpool dispatch (no async DB).

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from nexus.server.api.v2.dependencies import get_identity_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/agents", tags=["identity"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AgentKeyResponse(BaseModel):
    """Public key information."""

    key_id: str
    agent_id: str
    zone_id: str | None = None
    algorithm: str
    public_key_jwk: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class AgentIdentityResponse(BaseModel):
    """Identity verification response."""

    agent_id: str
    owner_id: str
    zone_id: str | None = None
    key_id: str
    algorithm: str
    public_key_jwk: dict[str, Any]
    created_at: datetime


class AgentKeyListResponse(BaseModel):
    """List of agent keys."""

    keys: list[AgentKeyResponse]


class AgentKeyRotateResponse(BaseModel):
    """Key rotation response."""

    new_key: AgentKeyResponse


class AgentKeyRevokeResponse(BaseModel):
    """Key revocation response."""

    key_id: str
    revoked_at: datetime


# ---------------------------------------------------------------------------
# Ownership helpers
# ---------------------------------------------------------------------------


def _check_ownership(agent_record: Any, auth_ctx: dict[str, Any], action: str) -> None:
    """Verify the authenticated user owns the agent (or is admin).

    Read endpoints (verify, list keys) are open to any authenticated user.
    Mutation endpoints (rotate, revoke) require ownership or admin.

    Raises:
        HTTPException(403) if the user is not the owner and not admin.
    """
    if auth_ctx.get("is_admin"):
        return

    owner_id = agent_record.owner_id
    user_id = auth_ctx.get("user_id", "")
    subject_id = auth_ctx.get("subject_id", "")

    # Owner match: user owns the agent
    if owner_id in (user_id, subject_id):
        return

    # Agent acting on itself: agent_id in subject_id
    if auth_ctx.get("subject_type") == "agent" and subject_id == agent_record.agent_id:
        return

    logger.warning(
        "[IDENTITY] Denied %s for agent '%s' by user_id=%s subject_id=%s",
        action,
        agent_record.agent_id,
        user_id,
        subject_id,
    )
    raise HTTPException(
        status_code=403,
        detail=f"Not authorized to {action} keys for agent '{agent_record.agent_id}'",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/verify")
def verify_agent_identity(
    agent_id: str,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_identity_context),
) -> AgentIdentityResponse:
    """Verify an agent's cryptographic identity.

    Returns the agent's active public key and owner information.
    Any authenticated user can verify any agent (public verification).
    """
    key_service, agent_registry, _auth_ctx = deps

    agent_record = agent_registry.get(agent_id)
    if agent_record is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    info = key_service.verify_identity(
        agent_id=agent_id,
        owner_id=agent_record.owner_id,
        zone_id=agent_record.zone_id,
    )
    if info is None:
        raise HTTPException(
            status_code=404,
            detail="Agent has no active cryptographic key",
        )

    return AgentIdentityResponse(
        agent_id=info.agent_id,
        owner_id=info.owner_id,
        zone_id=info.zone_id,
        key_id=info.key_id,
        algorithm=info.algorithm,
        public_key_jwk=info.public_key_jwk,
        created_at=info.created_at,
    )


@router.get("/{agent_id}/keys")
def list_agent_keys(
    agent_id: str,
    include_revoked: bool = False,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_identity_context),
) -> AgentKeyListResponse:
    """List all public keys for an agent.

    Any authenticated user can list public keys (they are public by design).
    """
    key_service, agent_registry, _auth_ctx = deps

    agent_record = agent_registry.get(agent_id)
    if agent_record is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    keys = key_service.list_keys(agent_id, include_revoked=include_revoked)
    return AgentKeyListResponse(
        keys=[
            AgentKeyResponse(
                key_id=k.key_id,
                agent_id=k.agent_id,
                zone_id=k.zone_id,
                algorithm=k.algorithm,
                public_key_jwk=k.public_key_jwk,
                created_at=k.created_at,
                expires_at=k.expires_at,
                revoked_at=k.revoked_at,
            )
            for k in keys
        ]
    )


@router.post("/{agent_id}/keys/rotate")
def rotate_agent_key(
    agent_id: str,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_identity_context),
) -> AgentKeyRotateResponse:
    """Generate a new key pair for an agent (key rotation).

    Requires ownership or admin. The old key is NOT automatically revoked.
    """
    key_service, agent_registry, auth_ctx = deps

    agent_record = agent_registry.get(agent_id)
    if agent_record is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    _check_ownership(agent_record, auth_ctx, "rotate")

    new_record = key_service.rotate_key(
        agent_id=agent_id,
        zone_id=agent_record.zone_id,
    )

    return AgentKeyRotateResponse(
        new_key=AgentKeyResponse(
            key_id=new_record.key_id,
            agent_id=new_record.agent_id,
            zone_id=new_record.zone_id,
            algorithm=new_record.algorithm,
            public_key_jwk=new_record.public_key_jwk,
            created_at=new_record.created_at,
            expires_at=new_record.expires_at,
            revoked_at=new_record.revoked_at,
        )
    )


@router.delete("/{agent_id}/keys/{key_id}")
def revoke_agent_key(
    agent_id: str,
    key_id: str,
    deps: tuple[Any, Any, dict[str, Any]] = Depends(get_identity_context),
) -> AgentKeyRevokeResponse:
    """Revoke an agent's key.

    Requires ownership or admin.
    """
    key_service, agent_registry, auth_ctx = deps

    agent_record = agent_registry.get(agent_id)
    if agent_record is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    _check_ownership(agent_record, auth_ctx, "revoke")

    result = key_service.revoke_key(agent_id, key_id)
    if not result:
        raise HTTPException(status_code=404, detail="Key not found or already revoked")

    revoked_key = key_service.get_public_key_by_key_id(key_id)
    if revoked_key is None or revoked_key.revoked_at is None:
        raise HTTPException(status_code=500, detail="Revocation succeeded but key state unexpected")

    return AgentKeyRevokeResponse(
        key_id=key_id,
        revoked_at=revoked_key.revoked_at,
    )
