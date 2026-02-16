"""Admin API domain client (async-only).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

import builtins
from typing import Any


class AsyncAdminClient:
    """Async Admin API client for managing API keys."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    async def create_key(
        self,
        user_id: str,
        name: str,
        zone_id: str = "default",
        is_admin: bool = False,
        expires_days: int | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
            "zone_id": zone_id,
            "is_admin": is_admin,
        }
        if expires_days is not None:
            params["expires_days"] = expires_days
        if subject_type is not None:
            params["subject_type"] = subject_type
        if subject_id is not None:
            params["subject_id"] = subject_id
        return await self._call_rpc("admin_create_key", params)  # type: ignore[no-any-return]

    async def list_keys(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool | None = None,
        include_expired: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"include_expired": include_expired}
        if user_id is not None:
            params["user_id"] = user_id
        if zone_id is not None:
            params["zone_id"] = zone_id
        if is_admin is not None:
            params["is_admin"] = is_admin
        result = await self._call_rpc("admin_list_keys", params)
        return result["keys"]  # type: ignore[no-any-return]

    async def get_key(self, key_id: str) -> dict[str, Any] | None:
        result = await self._call_rpc("admin_get_key", {"key_id": key_id})
        return result.get("key")  # type: ignore[no-any-return]

    async def revoke_key(self, key_id: str) -> bool:
        result = await self._call_rpc("admin_revoke_key", {"key_id": key_id})
        return result.get("success", False)  # type: ignore[no-any-return]

    async def update_key(
        self,
        key_id: str,
        name: str | None = None,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"key_id": key_id}
        if name is not None:
            params["name"] = name
        if expires_days is not None:
            params["expires_days"] = expires_days
        return await self._call_rpc("admin_update_key", params)  # type: ignore[no-any-return]
