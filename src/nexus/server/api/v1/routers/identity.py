"""Agent Identity API router (Issue #1355, #1288).

Provides agent cryptographic identity endpoints:
- GET   /api/agents/{agent_id}/identity — get agent public identity
- POST  /api/agents/{agent_id}/verify   — verify agent signature

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from nexus.server.api.v1.dependencies import get_key_service
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


@router.get("/api/agents/{agent_id}/identity")
async def get_agent_identity(
    agent_id: str,
    _auth_result: dict[str, Any] = Depends(require_auth),
    key_service: Any = Depends(get_key_service),
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


@router.post("/api/agents/{agent_id}/verify")
async def verify_agent_signature(
    agent_id: str,
    request: Request,
    _auth_result: dict[str, Any] = Depends(require_auth),
    key_service: Any = Depends(get_key_service),
) -> dict:
    """Verify a signature produced by an agent's signing key.

    Request body:
        {
            "message": "<base64-encoded message>",
            "signature": "<base64-encoded signature>",
            "key_id": "<optional key_id, uses newest active key if omitted>"
        }
    """
    body = await request.json()
    message_b64 = body.get("message")
    signature_b64 = body.get("signature")
    key_id = body.get("key_id")

    if not message_b64 or not signature_b64:
        raise HTTPException(
            status_code=400, detail="'message' and 'signature' are required (base64)"
        )

    try:
        message = base64.b64decode(message_b64)
        signature = base64.b64decode(signature_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding") from exc

    # Resolve public key
    resolved_key_id = key_id
    if key_id:
        record = await asyncio.to_thread(key_service.get_public_key, key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Key not found or not active")
        if record.agent_id != agent_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this agent")
        from nexus.identity.crypto import IdentityCrypto

        public_key = IdentityCrypto.public_key_from_bytes(record.public_key_bytes)
    else:
        keys = await asyncio.to_thread(key_service.get_active_keys, agent_id)
        if not keys:
            raise HTTPException(
                status_code=404, detail=f"No active keys for agent '{agent_id}'"
            )
        resolved_key_id = keys[0].key_id
        from nexus.identity.crypto import IdentityCrypto

        public_key = IdentityCrypto.public_key_from_bytes(keys[0].public_key_bytes)

    valid = key_service._crypto.verify(message, signature, public_key)

    return {"valid": valid, "agent_id": agent_id, "key_id": resolved_key_id}
