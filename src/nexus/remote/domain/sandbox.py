"""Sandbox management domain client (sync + async).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

from typing import Any, cast


class SandboxClient:
    """Sandbox management domain client (sync)."""

    def __init__(self, call_rpc: Any, get_server_url: Any, get_api_key: Any) -> None:
        self._call_rpc = call_rpc
        self._get_server_url = get_server_url
        self._get_api_key = get_api_key

    def connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
            "mount_path": mount_path,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        params["nexus_url"] = nexus_url or self._get_server_url()
        params["nexus_api_key"] = nexus_api_key or self._get_api_key()
        if agent_id is not None:
            params["agent_id"] = agent_id
        return cast(dict, self._call_rpc("sandbox_connect", params, read_timeout=60))

    def run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        as_script: bool = False,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "language": language,
            "code": code,
            "timeout": timeout,
        }
        if nexus_url is not None:
            params["nexus_url"] = nexus_url
        if nexus_api_key is not None:
            params["nexus_api_key"] = nexus_api_key
        if as_script:
            params["as_script"] = as_script
        return cast(dict, self._call_rpc("sandbox_run", params, read_timeout=timeout + 10))

    def pause(self, sandbox_id: str) -> dict:
        return self._call_rpc("sandbox_pause", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    def resume(self, sandbox_id: str) -> dict:
        return self._call_rpc("sandbox_resume", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    def stop(self, sandbox_id: str) -> dict:
        return self._call_rpc("sandbox_stop", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    def list(
        self,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"verify_status": verify_status}
        if user_id is not None:
            params["user_id"] = user_id
        if zone_id is not None:
            params["zone_id"] = zone_id
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        return self._call_rpc("sandbox_list", params)  # type: ignore[no-any-return]

    def status(self, sandbox_id: str) -> dict:
        return self._call_rpc("sandbox_status", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    def get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
    ) -> dict:
        params: dict[str, Any] = {
            "name": name,
            "ttl_minutes": ttl_minutes,
            "verify_status": verify_status,
        }
        if provider is not None:
            params["provider"] = provider
        if template_id is not None:
            params["template_id"] = template_id
        return cast(dict, self._call_rpc("sandbox_get_or_create", params))

    def disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        return self._call_rpc("sandbox_disconnect", params)  # type: ignore[no-any-return]

    def validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
    ) -> dict:
        return self._call_rpc(  # type: ignore[no-any-return]
            "sandbox_validate",
            {"sandbox_id": sandbox_id, "workspace_path": workspace_path},
        )


class AsyncSandboxClient:
    """Sandbox management domain client (async)."""

    def __init__(self, call_rpc: Any, get_server_url: Any, get_api_key: Any) -> None:
        self._call_rpc = call_rpc
        self._get_server_url = get_server_url
        self._get_api_key = get_api_key

    async def connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
            "mount_path": mount_path,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        params["nexus_url"] = nexus_url or self._get_server_url()
        params["nexus_api_key"] = nexus_api_key or self._get_api_key()
        if agent_id is not None:
            params["agent_id"] = agent_id
        return cast(dict, await self._call_rpc("sandbox_connect", params, read_timeout=60))

    async def run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        as_script: bool = False,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "language": language,
            "code": code,
            "timeout": timeout,
        }
        if nexus_url is not None:
            params["nexus_url"] = nexus_url
        if nexus_api_key is not None:
            params["nexus_api_key"] = nexus_api_key
        if as_script:
            params["as_script"] = as_script
        return cast(dict, await self._call_rpc("sandbox_run", params, read_timeout=timeout + 10))

    async def pause(self, sandbox_id: str) -> dict:
        return await self._call_rpc("sandbox_pause", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    async def resume(self, sandbox_id: str) -> dict:
        return await self._call_rpc("sandbox_resume", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    async def stop(self, sandbox_id: str) -> dict:
        return await self._call_rpc("sandbox_stop", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    async def list(
        self,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"verify_status": verify_status}
        if user_id is not None:
            params["user_id"] = user_id
        if zone_id is not None:
            params["zone_id"] = zone_id
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        return await self._call_rpc("sandbox_list", params)  # type: ignore[no-any-return]

    async def status(self, sandbox_id: str) -> dict:
        return await self._call_rpc("sandbox_status", {"sandbox_id": sandbox_id})  # type: ignore[no-any-return]

    async def get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
    ) -> dict:
        params: dict[str, Any] = {
            "name": name,
            "ttl_minutes": ttl_minutes,
            "verify_status": verify_status,
        }
        if provider is not None:
            params["provider"] = provider
        if template_id is not None:
            params["template_id"] = template_id
        return cast(dict, await self._call_rpc("sandbox_get_or_create", params))

    async def disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        return await self._call_rpc("sandbox_disconnect", params)  # type: ignore[no-any-return]

    async def validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
    ) -> dict:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "sandbox_validate",
            {"sandbox_id": sandbox_id, "workspace_path": workspace_path},
        )
