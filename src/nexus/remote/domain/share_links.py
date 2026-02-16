"""Share links domain client (sync + async).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

from typing import Any


class ShareLinksClient:
    """Share links domain client (sync)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    def create(
        self,
        path: str,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        max_access_count: int | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "create_share_link",
            {
                "path": path,
                "permission_level": permission_level,
                "expires_in_hours": expires_in_hours,
                "max_access_count": max_access_count,
                "password": password,
            },
        )

    def get(self, link_id: str) -> dict[str, Any]:
        return self._call_rpc("get_share_link", {"link_id": link_id})  # type: ignore[no-any-return]

    def list(
        self,
        path: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "list_share_links",
            {
                "path": path,
                "include_revoked": include_revoked,
                "include_expired": include_expired,
            },
        )

    def revoke(self, link_id: str) -> dict[str, Any]:
        return self._call_rpc("revoke_share_link", {"link_id": link_id})  # type: ignore[no-any-return]

    def access(
        self,
        link_id: str,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "access_share_link",
            {
                "link_id": link_id,
                "password": password,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )

    def get_access_logs(
        self,
        link_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "get_share_link_access_logs", {"link_id": link_id, "limit": limit}
        )


class AsyncShareLinksClient:
    """Share links domain client (async)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    async def create(
        self,
        path: str,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        max_access_count: int | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "create_share_link",
            {
                "path": path,
                "permission_level": permission_level,
                "expires_in_hours": expires_in_hours,
                "max_access_count": max_access_count,
                "password": password,
            },
        )

    async def get(self, link_id: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "get_share_link", {"link_id": link_id}
        )

    async def list(
        self,
        path: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "list_share_links",
            {
                "path": path,
                "include_revoked": include_revoked,
                "include_expired": include_expired,
            },
        )

    async def revoke(self, link_id: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "revoke_share_link", {"link_id": link_id}
        )

    async def access(
        self,
        link_id: str,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "access_share_link",
            {
                "link_id": link_id,
                "password": password,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )

    async def get_access_logs(
        self,
        link_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "get_share_link_access_logs", {"link_id": link_id, "limit": limit}
        )
