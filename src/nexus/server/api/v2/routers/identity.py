"""Agent Identity API v2 router (#2056).

Provides agent cryptographic identity endpoints:
- GET   /api/v2/agents/{agent_id}/identity — get agent public identity
- POST  /api/v2/agents/{agent_id}/verify   — verify agent signature

Ported from v1 with improvements:
- Pydantic request model for verify endpoint
- Top-level import of IdentityCrypto
- Generic error messages (don't leak internal exceptions)
"""

import asyncio
import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/agents", tags=["identity"])

# =============================================================================
# Request Models
# =============================================================================


class VerifySignatureRequest(BaseModel):
    """Request model for verifying an agent's signature."""

    message: str = Field(..., description="Base64-encoded message")
    signature: str = Field(..., description="Base64-encoded signature")
    key_id: str | None = Field(
        None, description="Optional key ID (uses newest active key if omitted)"
    )


# =============================================================================
# Dependencies
# =============================================================================


def _get_key_service(request: Request) -> Any:
    """Get KeyService from app.state, raising 503 if not available."""
    svc = getattr(request.app.state, "key_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Identity service not available")
    return svc


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/{agent_id}/identity")
async def get_agent_identity(
    agent_id: str,
    _auth_result: dict[str, Any] = Depends(require_auth),
    key_service: Any = Depends(_get_key_service),
) -> dict:
    """Get an agent's public identity (DID, public key, key_id).

    Returns the agent's active signing key information for verification.
    """
    keys = await asyncio.to_thread(key_service.get_active_keys, agent_id)
    if not keys:
        raise HTTPException(status_code=404, detail=f"No active keys for agent '{agent_id}'")

    newest = keys[0]
    return {
        "agent_id": agent_id,
        "key_id": newest.key_id,
        "did": newest.did,
        "algorithm": newest.algorithm,
        "public_key_hex": newest.public_key_bytes.hex(),
        "created_at": newest.created_at.isoformat() if newest.created_at else None,
        "expires_at": newest.expires_at.isoformat() if newest.expires_at else None,
    }


@router.post("/{agent_id}/verify")
async def verify_agent_signature(
    agent_id: str,
    body: VerifySignatureRequest,
    _auth_result: dict[str, Any] = Depends(require_auth),
    key_service: Any = Depends(_get_key_service),
) -> dict:
    """Verify a signature produced by an agent's signing key."""
    from nexus.bricks.identity.crypto import IdentityCrypto

    try:
        message = base64.b64decode(body.message)
        signature = base64.b64decode(body.signature)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding") from exc

    resolved_key_id = body.key_id
    if body.key_id:
        record = await asyncio.to_thread(key_service.get_public_key, body.key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Key not found or not active")
        if record.agent_id != agent_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this agent")
        public_key = IdentityCrypto.public_key_from_bytes(record.public_key_bytes)
    else:
        keys = await asyncio.to_thread(key_service.get_active_keys, agent_id)
        if not keys:
            raise HTTPException(status_code=404, detail=f"No active keys for agent '{agent_id}'")
        resolved_key_id = keys[0].key_id
        public_key = IdentityCrypto.public_key_from_bytes(keys[0].public_key_bytes)

    valid = key_service._crypto.verify(message, signature, public_key)

    return {"valid": valid, "agent_id": agent_id, "key_id": resolved_key_id}
