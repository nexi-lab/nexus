"""Sandbox RPC Service — replaces NexusFS sandbox management facades.

Wraps SandboxManager with @rpc_expose + context parsing.
All methods are async (RPC server detects and handles this).

Issue #2033 — Phase 2.3 of LEGO microkernel decomposition.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose
from nexus.contracts.types import OperationContext, parse_operation_context

logger = logging.getLogger(__name__)


class SandboxRPCService:
    """RPC surface for sandbox management operations.

    Replaces ~480 LOC of facades in NexusFS (sandbox_create, sandbox_run, etc.).
    Lazy-initializes SandboxManager on first use.
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        default_context: OperationContext,
        config: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._default_context = default_context
        self._config = config
        self._sandbox_manager: Any | None = None

    def _ensure_sandbox_manager(self) -> None:
        """Lazily initialize SandboxManager."""
        if self._sandbox_manager is not None:
            return

        import os

        from nexus.sandbox.sandbox_manager import SandboxManager

        self._sandbox_manager = SandboxManager(
            session_factory=self._session_factory,
            e2b_api_key=os.getenv("E2B_API_KEY"),
            e2b_team_id=os.getenv("E2B_TEAM_ID"),
            e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
            config=self._config,
        )

        if self._sandbox_manager.providers:
            from nexus.sandbox.sandbox_router import SandboxRouter

            self._sandbox_manager._router = SandboxRouter(
                available_providers=self._sandbox_manager.providers,
            )

    @property
    def sandbox_available(self) -> bool:
        """Whether sandbox execution is available."""
        try:
            self._ensure_sandbox_manager()
        except Exception:
            return False
        return bool(self._sandbox_manager and self._sandbox_manager.providers)

    # ------------------------------------------------------------------
    # Public RPC Methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Create a new sandbox")
    async def sandbox_create(
        self, name: str, ttl_minutes: int = 10, provider: str | None = None,
        template_id: str | None = None, context: dict | None = None,
    ) -> dict:
        """Create a new code execution sandbox."""
        ctx = parse_operation_context(context)
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.create_sandbox(
            name=name,
            user_id=ctx.user_id or "system",
            zone_id=ctx.zone_id or self._default_context.zone_id or "root",
            agent_id=ctx.agent_id,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
        )
        return result

    @rpc_expose(description="Run code in sandbox")
    async def sandbox_run(
        self, sandbox_id: str, language: str, code: str, timeout: int = 300,
        nexus_url: str | None = None, nexus_api_key: str | None = None,
        context: dict | None = None, as_script: bool = False,
    ) -> dict:
        """Run code in a sandbox."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        if not nexus_api_key and context:
            ctx = parse_operation_context(context)
            nexus_api_key = getattr(ctx, "api_key", None)

        if not nexus_url:
            import os
            nexus_url = os.getenv("NEXUS_SERVER_URL") or os.getenv("NEXUS_URL")

        # Inject credentials as environment variables
        if nexus_url or nexus_api_key:
            code = self._inject_env_vars(language, code, nexus_url, nexus_api_key)

        import dataclasses

        execution_result = await self._sandbox_manager.run_code(
            sandbox_id, language, code, timeout, as_script=as_script,
        )
        result = dataclasses.asdict(execution_result)
        if result.get("validations"):
            result["validations"] = [
                v.model_dump() if hasattr(v, "model_dump") else v
                for v in result["validations"]
            ]
        return result

    @rpc_expose(description="Validate code in sandbox")
    async def sandbox_validate(
        self, sandbox_id: str, workspace_path: str = "/workspace",
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Run validation pipeline in sandbox."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None
        results = await self._sandbox_manager.validate(sandbox_id, workspace_path)
        return {"validations": results}

    @rpc_expose(description="Pause sandbox")
    async def sandbox_pause(
        self, sandbox_id: str, context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Pause sandbox to save costs."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None
        result: dict[Any, Any] = await self._sandbox_manager.pause_sandbox(sandbox_id)
        return result

    @rpc_expose(description="Resume paused sandbox")
    async def sandbox_resume(
        self, sandbox_id: str, context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Resume a paused sandbox."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None
        result: dict[Any, Any] = await self._sandbox_manager.resume_sandbox(sandbox_id)
        return result

    @rpc_expose(description="Stop and destroy sandbox")
    async def sandbox_stop(
        self, sandbox_id: str, context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Stop and destroy sandbox."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None
        result: dict[Any, Any] = await self._sandbox_manager.stop_sandbox(sandbox_id)
        return result

    @rpc_expose(description="List sandboxes")
    async def sandbox_list(
        self, context: dict | None = None, verify_status: bool = False,
        user_id: str | None = None, zone_id: str | None = None,
        agent_id: str | None = None, status: str | None = None,
    ) -> dict:
        """List user's sandboxes."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        ctx = parse_operation_context(context)
        filter_user_id = user_id if (user_id is not None and ctx.is_admin) else ctx.user_id
        filter_zone_id = zone_id if (zone_id is not None and ctx.is_admin) else ctx.zone_id
        filter_agent_id = agent_id if agent_id is not None else ctx.agent_id

        sandboxes = await self._sandbox_manager.list_sandboxes(
            user_id=filter_user_id, zone_id=filter_zone_id,
            agent_id=filter_agent_id, status=status, verify_status=verify_status,
        )
        return {"sandboxes": sandboxes}

    @rpc_expose(description="Get sandbox status")
    async def sandbox_status(
        self, sandbox_id: str, context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Get sandbox status and metadata."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None
        result: dict[Any, Any] = await self._sandbox_manager.get_sandbox_status(sandbox_id)
        return result

    @rpc_expose(description="Get or create sandbox")
    async def sandbox_get_or_create(
        self, name: str, ttl_minutes: int = 10, provider: str | None = None,
        template_id: str | None = None, verify_status: bool = True,
        context: dict | None = None,
    ) -> dict:
        """Get existing active sandbox or create a new one."""
        ctx = parse_operation_context(context)
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.get_or_create_sandbox(
            name=name,
            user_id=ctx.user_id or "system",
            zone_id=ctx.zone_id or self._default_context.zone_id or "root",
            agent_id=ctx.agent_id,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
            verify_status=verify_status,
        )
        return result

    @rpc_expose(description="Connect to user-managed sandbox")
    async def sandbox_connect(
        self, sandbox_id: str, provider: str = "e2b",
        sandbox_api_key: str | None = None, mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None, nexus_api_key: str | None = None,
        agent_id: str | None = None, context: dict | None = None,
    ) -> dict:
        """Connect and mount Nexus to a sandbox."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        if not nexus_url:
            import os
            nexus_url = os.getenv("NEXUS_SERVER_URL") or os.getenv(
                "NEXUS_URL", "http://localhost:2026",
            )

        if not nexus_api_key:
            ctx = parse_operation_context(context)
            nexus_api_key = getattr(ctx, "api_key", None)

        if not nexus_api_key:
            raise ValueError(
                "Nexus API key required for mounting. Pass nexus_api_key or provide in context."
            )

        result: dict[Any, Any] = await self._sandbox_manager.connect_sandbox(
            sandbox_id=sandbox_id, provider=provider, sandbox_api_key=sandbox_api_key,
            mount_path=mount_path, nexus_url=nexus_url, nexus_api_key=nexus_api_key,
            agent_id=agent_id,
        )
        return result

    @rpc_expose(description="Disconnect from user-managed sandbox")
    async def sandbox_disconnect(
        self, sandbox_id: str, provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Disconnect and unmount Nexus from a sandbox."""
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.disconnect_sandbox(
            sandbox_id=sandbox_id, provider=provider, sandbox_api_key=sandbox_api_key,
        )
        return result

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_env_vars(
        language: str, code: str,
        nexus_url: str | None, nexus_api_key: str | None,
    ) -> str:
        """Inject Nexus credentials as environment variables into code."""
        if language == "bash":
            lines = []
            if nexus_url:
                lines.append(f'export NEXUS_URL="{nexus_url}"')
            if nexus_api_key:
                lines.append(f'export NEXUS_API_KEY="{nexus_api_key}"')
            return "\n".join(lines) + "\n" + code if lines else code
        elif language == "python":
            lines = ["import os"]
            if nexus_url:
                lines.append(f'os.environ["NEXUS_URL"] = "{nexus_url}"')
            if nexus_api_key:
                lines.append(f'os.environ["NEXUS_API_KEY"] = "{nexus_api_key}"')
            return "\n".join(lines) + "\n" + code
        elif language in ("javascript", "js"):
            lines = []
            if nexus_url:
                lines.append(f'process.env.NEXUS_URL = "{nexus_url}";')
            if nexus_api_key:
                lines.append(f'process.env.NEXUS_API_KEY = "{nexus_api_key}";')
            return "\n".join(lines) + "\n" + code if lines else code
        return code
