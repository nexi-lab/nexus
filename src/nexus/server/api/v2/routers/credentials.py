"""Agent Verifiable Credentials API v2 router (Issue #1753).

Provides JWT-VC credential lifecycle endpoints:
- POST  /api/v2/credentials/issue             — issue a capability credential
- POST  /api/v2/credentials/verify            — verify a JWT-VC token
- DELETE /api/v2/credentials/{credential_id}  — revoke a credential
- GET   /api/v2/credentials/{credential_id}   — get credential status
- GET   /api/v2/agents/{agent_id}/credentials — list agent credentials

All endpoints require authentication.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.contracts.credential_types import Ability, Capability
from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)


def _authorize_agent_credential_access(
    auth_result: dict[str, Any],
    agent_id: str,
    action: str = "access",
) -> None:
    """Verify caller is authorized to manage an agent's credentials."""
    ctx = get_operation_context(auth_result)
    if ctx.is_admin:
        return
    if ctx.subject_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"Not authorized to {action} credentials for agent '{agent_id}'",
        )


router = APIRouter(tags=["credentials"])

# =============================================================================
# Request / Response Models
# =============================================================================


class CapabilityRequest(BaseModel):
    """A single capability in an issue request."""

    resource: str = Field(..., description="URI of the target resource (e.g. 'nexus:brick:search')")
    abilities: list[str] = Field(
        ..., description="List of abilities (read, write, execute, delegate, *)"
    )
    caveats: dict[str, Any] = Field(default_factory=dict, description="Optional constraints")


class IssueCredentialRequest(BaseModel):
    """Request to issue a capability credential."""

    agent_id: str = Field(..., description="Agent ID to receive the credential")
    capabilities: list[CapabilityRequest] = Field(
        ..., min_length=1, description="Capabilities to grant"
    )
    ttl_seconds: int = Field(3600, ge=60, le=86400, description="TTL in seconds (60-86400)")


class VerifyCredentialRequest(BaseModel):
    """Request to verify a JWT-VC token."""

    token: str = Field(..., description="JWT-VC token string")


class DelegateCredentialRequest(BaseModel):
    """Request to delegate capabilities to another agent."""

    parent_token: str = Field(..., description="JWT-VC of the parent credential")
    delegate_agent_id: str = Field(..., description="Agent ID to receive delegated capabilities")
    capabilities: list[CapabilityRequest] = Field(
        ..., min_length=1, description="Attenuated capabilities (must be subset of parent)"
    )
    ttl_seconds: int = Field(1800, ge=60, le=86400, description="TTL in seconds")


# =============================================================================
# Dependencies
# =============================================================================


def _get_credential_service(request: Request) -> Any:
    """Get CredentialService from app.state, raising 503 if not available."""
    svc = getattr(request.app.state, "credential_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Credential service not available")
    return svc


def _get_key_service(request: Request) -> Any:
    """Get KeyService from app.state, raising 503 if not available."""
    svc = getattr(request.app.state, "key_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Identity service not available")
    return svc


def _parse_capabilities(raw: list[CapabilityRequest]) -> list[Capability]:
    """Convert Pydantic models to domain Capability objects."""
    import types

    result = []
    for cap_req in raw:
        abilities = tuple(Ability(a) for a in cap_req.abilities)
        caveats = (
            types.MappingProxyType(cap_req.caveats)
            if cap_req.caveats
            else types.MappingProxyType({})
        )
        result.append(Capability(resource=cap_req.resource, abilities=abilities, caveats=caveats))
    return result


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/api/v2/credentials/issue")
async def issue_credential(
    body: IssueCredentialRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    credential_service: Any = Depends(_get_credential_service),
    key_service: Any = Depends(_get_key_service),
) -> dict:
    """Issue a JWT-VC capability credential to an agent.

    Requires authentication. The caller must own the target agent (matching
    subject_id) or be an admin.
    """
    _authorize_agent_credential_access(auth_result, body.agent_id, "issue")
    # Resolve agent's DID
    keys = await asyncio.to_thread(key_service.get_active_keys, body.agent_id)
    if not keys:
        raise HTTPException(
            status_code=404,
            detail=f"No identity found for agent '{body.agent_id}'. Register the agent first.",
        )

    subject_did = keys[0].did

    try:
        capabilities = _parse_capabilities(body.capabilities)
        claims = await asyncio.to_thread(
            credential_service.issue_credential,
            body.agent_id,
            subject_did,
            capabilities,
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "credential_id": claims.credential_id,
        "issuer_did": claims.issuer_did,
        "subject_did": claims.subject_did,
        "backend_features": [cap.to_dict() for cap in claims.capabilities],
        "issued_at": claims.issued_at,
        "expires_at": claims.expires_at,
        "delegation_depth": claims.delegation_depth,
    }


@router.post("/api/v2/credentials/verify")
async def verify_credential(
    body: VerifyCredentialRequest,
    _auth_result: dict[str, Any] = Depends(require_auth),
    credential_service: Any = Depends(_get_credential_service),
) -> dict:
    """Verify a JWT-VC capability credential.

    Returns the parsed claims if valid, or an error if invalid.
    """
    try:
        claims = await asyncio.to_thread(
            credential_service.verify_credential,
            body.token,
        )
    except ValueError as exc:
        return {"valid": False, "error": str(exc)}

    return {
        "valid": True,
        "credential_id": claims.credential_id,
        "issuer_did": claims.issuer_did,
        "subject_did": claims.subject_did,
        "backend_features": [cap.to_dict() for cap in claims.capabilities],
        "expires_at": claims.expires_at,
        "delegation_depth": claims.delegation_depth,
    }


@router.delete("/api/v2/credentials/{credential_id}")
async def revoke_credential(
    credential_id: str,
    _auth_result: dict[str, Any] = Depends(require_auth),
    credential_service: Any = Depends(_get_credential_service),
) -> dict:
    """Revoke a credential by ID."""
    revoked = await asyncio.to_thread(
        credential_service.revoke_credential,
        credential_id,
    )

    if not revoked:
        raise HTTPException(status_code=404, detail="Credential not found")

    return {"revoked": True, "credential_id": credential_id}


@router.get("/api/v2/credentials/{credential_id}")
async def get_credential_status(
    credential_id: str,
    _auth_result: dict[str, Any] = Depends(require_auth),
    credential_service: Any = Depends(_get_credential_service),
) -> dict:
    """Get the status of a credential."""
    status = await asyncio.to_thread(
        credential_service.get_credential_status,
        credential_id,
    )

    if status is None:
        raise HTTPException(status_code=404, detail="Credential not found")

    return {
        "credential_id": status.credential_id,
        "issuer_did": status.issuer_did,
        "subject_did": status.subject_did,
        "subject_agent_id": status.subject_agent_id,
        "is_active": status.is_active,
        "created_at": status.created_at.isoformat() if status.created_at else None,
        "expires_at": status.expires_at.isoformat() if status.expires_at else None,
        "revoked_at": status.revoked_at.isoformat() if status.revoked_at else None,
        "delegation_depth": status.delegation_depth,
        "parent_credential_id": status.parent_credential_id,
    }


@router.get("/api/v2/agents/{agent_id}/credentials")
async def list_agent_credentials(
    agent_id: str,
    active_only: bool = True,
    auth_result: dict[str, Any] = Depends(require_auth),
    credential_service: Any = Depends(_get_credential_service),
) -> dict:
    """List credentials for an agent."""
    _authorize_agent_credential_access(auth_result, agent_id, "list")
    credentials = await asyncio.to_thread(
        credential_service.list_agent_credentials,
        agent_id,
        active_only=active_only,
    )

    return {
        "agent_id": agent_id,
        "count": len(credentials),
        "credentials": [
            {
                "credential_id": c.credential_id,
                "issuer_did": c.issuer_did,
                "subject_did": c.subject_did,
                "is_active": c.is_active,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
                "delegation_depth": c.delegation_depth,
            }
            for c in credentials
        ],
    }


@router.post("/api/v2/credentials/delegate")
async def delegate_credential(
    body: DelegateCredentialRequest,
    _auth_result: dict[str, Any] = Depends(require_auth),
    credential_service: Any = Depends(_get_credential_service),
    key_service: Any = Depends(_get_key_service),
) -> dict:
    """Delegate attenuated capabilities to another agent.

    The delegated capabilities must be a subset of the parent credential.
    Caller must own the parent credential (verified by service via token claims).
    """
    # Resolve delegate's DID
    keys = await asyncio.to_thread(key_service.get_active_keys, body.delegate_agent_id)
    if not keys:
        raise HTTPException(
            status_code=404,
            detail=f"No identity found for delegate agent '{body.delegate_agent_id}'.",
        )

    delegate_did = keys[0].did

    try:
        capabilities = _parse_capabilities(body.capabilities)
        claims = await asyncio.to_thread(
            credential_service.delegate_credential,
            body.parent_token,
            body.delegate_agent_id,
            delegate_did,
            capabilities,
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "credential_id": claims.credential_id,
        "issuer_did": claims.issuer_did,
        "subject_did": claims.subject_did,
        "backend_features": [cap.to_dict() for cap in claims.capabilities],
        "expires_at": claims.expires_at,
        "delegation_depth": claims.delegation_depth,
        "parent_credential_id": claims.parent_credential_id,
    }
