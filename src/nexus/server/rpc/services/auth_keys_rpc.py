"""Auth Keys RPC Service — API key lifecycle management."""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class AuthKeysRPCService:
    """RPC surface for API key management operations."""

    def __init__(self, record_store: Any, rebac_manager: Any | None = None) -> None:
        self._record_store = record_store
        self._rebac_manager = rebac_manager

    def _get_db_auth(self) -> Any:
        from nexus.server.auth.database_auth import DatabaseAPIKeyAuth

        return DatabaseAPIKeyAuth(self._record_store)

    @rpc_expose(description="Create an API key", admin_only=True)
    async def auth_keys_create(
        self,
        zone_id: str = "root",
        label: str | None = None,
        name: str | None = None,
        user_id: str | None = None,
        subject_type: str = "user",
        subject_id: str | None = None,
        is_admin: bool = False,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        db_auth = self._get_db_auth()
        result = db_auth.create_key(
            zone_id=zone_id,
            label=label,
            name=name,
            user_id=user_id,
            subject_type=subject_type,
            subject_id=subject_id,
            is_admin=is_admin,
            expires_days=expires_days,
        )
        return {
            "key_id": result.key_id,
            "api_key": result.api_key,
            "zone_id": zone_id,
        }

    @rpc_expose(description="List API keys", admin_only=True)
    async def auth_keys_list(
        self,
        zone_id: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        db_auth = self._get_db_auth()
        keys = db_auth.list_keys(
            zone_id=zone_id,
            include_revoked=include_revoked,
            include_expired=include_expired,
            limit=limit,
            offset=offset,
        )
        return {
            "keys": [
                {
                    "key_id": k.key_id,
                    "name": k.name,
                    "zone_id": k.zone_id,
                    "subject_type": k.subject_type,
                    "subject_id": k.subject_id,
                    "is_admin": k.is_admin,
                    "revoked": k.revoked,
                }
                for k in keys
            ],
            "count": len(keys),
        }

    @rpc_expose(description="Get API key details", admin_only=True)
    async def auth_keys_get(self, key_id: str) -> dict[str, Any]:
        db_auth = self._get_db_auth()
        key = db_auth.get_key(key_id)
        if key is None:
            return {"error": f"Key {key_id} not found"}
        return {
            "key_id": key.key_id,
            "name": key.name,
            "zone_id": key.zone_id,
            "subject_type": key.subject_type,
            "subject_id": key.subject_id,
            "is_admin": key.is_admin,
        }

    @rpc_expose(description="Revoke an API key", admin_only=True)
    async def auth_keys_revoke(self, key_id: str) -> dict[str, Any]:
        db_auth = self._get_db_auth()
        revoked = db_auth.revoke_key(key_id)
        return {"revoked": revoked, "key_id": key_id}
