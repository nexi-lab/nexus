"""Synchronous RPC facade for sandbox lifecycle operations.

The sandbox brick exposes async provider and manager APIs. CLI, MCP, and RPC
surfaces are mostly synchronous, so this facade is the service-registry boundary
that bridges sync callers to ``SandboxManager``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from typing import Any, TypedDict, cast

from nexus.bricks.sandbox.sandbox_manager import SandboxManager
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.sync_bridge import run_sync


class _RecordStoreShim:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory


class _SandboxIdentity(TypedDict):
    user_id: str
    zone_id: str
    agent_id: str | None


def _dictify(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_dictify(item) for item in value]
    if isinstance(value, dict):
        return {key: _dictify(item) for key, item in value.items()}
    return value


def _dict_result(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], _dictify(value))


def _dict_list_result(value: Any) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _dictify(value))


class SandboxRPCService:
    """Sync service-registry API for sandbox management."""

    def __init__(
        self,
        *,
        session_factory: Any,
        default_context: Any = None,
        config: Any = None,
        manager: SandboxManager | None = None,
    ) -> None:
        self._default_context = default_context
        self._manager = manager or SandboxManager(
            record_store=cast(Any, _RecordStoreShim(session_factory)),
            e2b_api_key=os.getenv("E2B_API_KEY"),
            e2b_team_id=os.getenv("E2B_TEAM_ID"),
            e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
            config=config,
        )

    def available_providers(self) -> list[str]:
        registry = getattr(self._manager, "_registry", None)
        if registry is None:
            return []
        return list(registry.available_names())

    def is_available(self) -> bool:
        return bool(self.available_providers())

    def sandbox_create(
        self,
        *,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        identity = self._identity(context)
        result = run_sync(
            self._manager.create_sandbox(
                name=name,
                user_id=identity["user_id"],
                zone_id=identity["zone_id"],
                agent_id=identity["agent_id"],
                ttl_minutes=ttl_minutes,
                provider=provider,
                template_id=template_id,
            ),
            timeout=120.0,
        )
        return _dict_result(result)

    def sandbox_get_or_create(
        self,
        *,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: Any = None,
    ) -> dict[str, Any]:
        identity = self._identity(context)
        result = run_sync(
            self._manager.get_or_create_sandbox(
                name=name,
                user_id=identity["user_id"],
                zone_id=identity["zone_id"],
                agent_id=identity["agent_id"],
                ttl_minutes=ttl_minutes,
                provider=provider,
                template_id=template_id,
                verify_status=verify_status,
            ),
            timeout=120.0,
        )
        return _dict_result(result)

    def sandbox_run(
        self,
        *,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        context: Any = None,  # noqa: ARG002
        as_script: bool = False,
    ) -> dict[str, Any]:
        result = run_sync(
            self._manager.run_code(
                sandbox_id=sandbox_id,
                language=language,
                code=code,
                timeout=timeout,
                as_script=as_script,
            ),
            timeout=timeout + 5.0,
        )
        return _dict_result(result)

    def sandbox_pause(self, *, sandbox_id: str, context: Any = None) -> dict[str, Any]:
        _ = context
        return _dict_result(run_sync(self._manager.pause_sandbox(sandbox_id), timeout=30.0))

    def sandbox_resume(self, *, sandbox_id: str, context: Any = None) -> dict[str, Any]:
        _ = context
        return _dict_result(run_sync(self._manager.resume_sandbox(sandbox_id), timeout=30.0))

    def sandbox_stop(self, sandbox_id: str, context: Any = None) -> dict[str, Any]:  # noqa: ARG002
        return _dict_result(run_sync(self._manager.stop_sandbox(sandbox_id), timeout=30.0))

    def sandbox_list(
        self,
        *,
        context: Any = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        identity = self._identity(context)
        result = run_sync(
            self._manager.list_sandboxes(
                user_id=user_id or identity["user_id"],
                zone_id=zone_id,
                agent_id=agent_id,
                status=status,
                verify_status=verify_status,
            ),
            timeout=30.0,
        )
        return _dict_list_result(result)

    def sandbox_status(self, *, sandbox_id: str, context: Any = None) -> dict[str, Any]:
        _ = context
        return _dict_result(run_sync(self._manager.get_sandbox_status(sandbox_id), timeout=30.0))

    def sandbox_connect(
        self,
        *,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        return _dict_result(
            run_sync(
                self._manager.connect_sandbox(
                    sandbox_id=sandbox_id,
                    provider=provider,
                    sandbox_api_key=sandbox_api_key,
                    mount_path=mount_path,
                    nexus_url=nexus_url,
                    nexus_api_key=nexus_api_key,
                    agent_id=agent_id,
                ),
                timeout=120.0,
            )
        )

    def sandbox_disconnect(
        self,
        *,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        return _dict_result(
            run_sync(
                self._manager.disconnect_sandbox(
                    sandbox_id=sandbox_id,
                    provider=provider,
                    sandbox_api_key=sandbox_api_key,
                ),
                timeout=30.0,
            )
        )

    def _identity(self, context: Any = None) -> _SandboxIdentity:
        source = context or self._default_context
        user_id = self._get(source, "user_id") or "system"
        zone_id = self._get(source, "zone_id") or ROOT_ZONE_ID
        agent_id = self._get(source, "agent_id")
        return {
            "user_id": str(user_id),
            "zone_id": str(zone_id),
            "agent_id": str(agent_id) if agent_id is not None else None,
        }

    @staticmethod
    def _get(source: Any, key: str) -> Any:
        if source is None:
            return None
        if isinstance(source, dict):
            return source.get(key)
        return getattr(source, key, None)
