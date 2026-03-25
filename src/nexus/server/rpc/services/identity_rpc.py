"""Identity RPC Service — agent cryptographic identity.

Issue #2056.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class IdentityRPCService:
    """RPC surface for agent identity operations."""

    def __init__(self, key_service: Any) -> None:
        self._key_service = key_service

    @rpc_expose(description="Get agent cryptographic identity")
    async def identity_show(self, agent_id: str) -> dict[str, Any]:
        import asyncio

        keys = await asyncio.to_thread(self._key_service.get_active_keys, agent_id)
        if not keys:
            return {"error": f"No identity found for agent '{agent_id}'"}
        key = keys[0]
        return {
            "agent_id": agent_id,
            "did": key.did,
            "key_id": key.key_id,
            "algorithm": key.algorithm,
            "public_key_hex": key.public_key_hex,
            "created_at": key.created_at.isoformat() if key.created_at else None,
        }

    @rpc_expose(description="Verify an agent's Ed25519 signature")
    async def identity_verify(
        self,
        agent_id: str,
        message: str,
        signature: str,
        key_id: str | None = None,
    ) -> dict[str, Any]:
        import asyncio

        keys = await asyncio.to_thread(self._key_service.get_active_keys, agent_id)
        if not keys:
            return {"valid": False, "error": f"No identity found for agent '{agent_id}'"}

        key = keys[0] if key_id is None else next((k for k in keys if k.key_id == key_id), None)
        if key is None:
            return {"valid": False, "error": f"Key '{key_id}' not found"}

        from nexus.bricks.identity.crypto import IdentityCrypto

        try:
            crypto = IdentityCrypto()
            pub_key = IdentityCrypto.public_key_from_bytes(bytes.fromhex(key.public_key_hex))
            is_valid = crypto.verify(
                message=message.encode(),
                signature=bytes.fromhex(signature),
                public_key=pub_key,
            )
            return {"valid": is_valid, "key_id": key.key_id, "did": key.did}
        except Exception as e:
            return {"valid": False, "error": str(e)}
