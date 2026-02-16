"""MCP (Model Context Protocol) domain client (sync + async).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

import builtins
from typing import Any


class MCPClient:
    """MCP management domain client (sync)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    def connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        user_email: str | None = None,
        reuse_nexus_token: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": provider,
            "reuse_nexus_token": reuse_nexus_token,
        }
        if redirect_url is not None:
            params["redirect_url"] = redirect_url
        if user_email is not None:
            params["user_email"] = user_email
        return self._call_rpc("mcp_connect", params)  # type: ignore[no-any-return]

    def get_oauth_url(
        self,
        provider: str,
        redirect_url: str,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "mcp_get_oauth_url",
            {"provider": provider, "redirect_url": redirect_url},
        )

    def list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"include_unmounted": include_unmounted}
        if tier is not None:
            params["tier"] = tier
        return self._call_rpc("mcp_list_mounts", params)  # type: ignore[no-any-return]

    def list_tools(self, name: str) -> builtins.list[dict[str, Any]]:
        return self._call_rpc("mcp_list_tools", {"name": name})  # type: ignore[no-any-return]

    def mount(
        self,
        name: str,
        transport: str | None = None,
        command: str | None = None,
        url: str | None = None,
        args: builtins.list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        tier: str = "system",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "tier": tier}
        if transport is not None:
            params["transport"] = transport
        if command is not None:
            params["command"] = command
        if url is not None:
            params["url"] = url
        if args is not None:
            params["args"] = args
        if env is not None:
            params["env"] = env
        if headers is not None:
            params["headers"] = headers
        if description is not None:
            params["description"] = description
        return self._call_rpc("mcp_mount", params)  # type: ignore[no-any-return]

    def unmount(self, name: str) -> dict[str, Any]:
        return self._call_rpc("mcp_unmount", {"name": name})  # type: ignore[no-any-return]

    def sync(self, name: str) -> dict[str, Any]:
        return self._call_rpc("mcp_sync", {"name": name})  # type: ignore[no-any-return]

    def backfill_directory_index(
        self,
        prefix: str = "/",
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "backfill_directory_index", {"prefix": prefix, "zone_id": zone_id}
        )


class AsyncMCPClient:
    """MCP management domain client (async)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    async def connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        user_email: str | None = None,
        reuse_nexus_token: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": provider,
            "reuse_nexus_token": reuse_nexus_token,
        }
        if redirect_url is not None:
            params["redirect_url"] = redirect_url
        if user_email is not None:
            params["user_email"] = user_email
        return await self._call_rpc("mcp_connect", params)  # type: ignore[no-any-return]

    async def get_oauth_url(
        self,
        provider: str,
        redirect_url: str,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "mcp_get_oauth_url",
            {"provider": provider, "redirect_url": redirect_url},
        )

    async def list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"include_unmounted": include_unmounted}
        if tier is not None:
            params["tier"] = tier
        return await self._call_rpc("mcp_list_mounts", params)  # type: ignore[no-any-return]

    async def list_tools(self, name: str) -> builtins.list[dict[str, Any]]:
        return await self._call_rpc("mcp_list_tools", {"name": name})  # type: ignore[no-any-return]

    async def mount(
        self,
        name: str,
        transport: str | None = None,
        command: str | None = None,
        url: str | None = None,
        args: builtins.list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        tier: str = "system",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "tier": tier}
        if transport is not None:
            params["transport"] = transport
        if command is not None:
            params["command"] = command
        if url is not None:
            params["url"] = url
        if args is not None:
            params["args"] = args
        if env is not None:
            params["env"] = env
        if headers is not None:
            params["headers"] = headers
        if description is not None:
            params["description"] = description
        return await self._call_rpc("mcp_mount", params)  # type: ignore[no-any-return]

    async def unmount(self, name: str) -> dict[str, Any]:
        return await self._call_rpc("mcp_unmount", {"name": name})  # type: ignore[no-any-return]

    async def sync(self, name: str) -> dict[str, Any]:
        return await self._call_rpc("mcp_sync", {"name": name})  # type: ignore[no-any-return]

    async def backfill_directory_index(
        self,
        prefix: str = "/",
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "backfill_directory_index", {"prefix": prefix, "zone_id": zone_id}
        )
