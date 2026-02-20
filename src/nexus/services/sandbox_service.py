"""Sandbox Service — extracted from NexusFS kernel (Task #637).

Encapsulates:
- Sandbox lifecycle operations (create, pause, resume, stop, list, status)
- Code execution (run, validate)
- Sandbox connection/disconnection (FUSE mount/unmount)
- Get-or-create idempotent pattern

All service-layer imports (SandboxManager, SandboxRouter) stay here —
the kernel never sees them.

RPC methods are decorated with ``@rpc_expose`` so they are auto-discovered
by ``_discover_exposed_methods()`` when passed as an additional source.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import TYPE_CHECKING, Any

from nexus.contracts.types import OperationContext
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _parse_context(context: OperationContext | dict | None) -> OperationContext:
    """Parse context dict or OperationContext into OperationContext."""
    if isinstance(context, OperationContext):
        return context
    if context is None:
        context = {}
    return OperationContext(
        user_id=context.get("user_id", "system"),
        groups=context.get("groups", []),
        zone_id=context.get("zone_id"),
        agent_id=context.get("agent_id"),
        is_admin=context.get("is_admin", False),
        is_system=context.get("is_system", False),
    )


class SandboxService:
    """Sandbox lifecycle operations (ASYNC).

    Wraps SandboxManager creation and all sandbox CRUD + code execution
    operations.  Constructed via ``create_sandbox_service()`` and registered
    as an additional RPC source in
    ``fastapi_server._discover_exposed_methods()``.

    The kernel never imports or calls this service.
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        default_context: OperationContext,
        config: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._default_context = default_context
        self._config = config
        self._sandbox_manager: Any = None

    # ── Internal helpers ──────────────────────────────────────────────

    def _ensure_sandbox_manager(self) -> None:
        """Ensure sandbox manager is initialized (lazy initialization)."""
        if self._sandbox_manager is None:
            from nexus.bricks.sandbox.sandbox_manager import SandboxManager

            self._sandbox_manager = SandboxManager(
                session_factory=self._session_factory,
                e2b_api_key=os.getenv("E2B_API_KEY"),
                e2b_team_id=os.getenv("E2B_TEAM_ID"),
                e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
                config=self._config,
            )

            # Attach smart router if providers are available (Issue #1317)
            if self._sandbox_manager.providers:
                from nexus.bricks.sandbox.sandbox_router import SandboxRouter

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

    # ── Sandbox CRUD operations ───────────────────────────────────────

    @rpc_expose(description="Create a new sandbox")
    async def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Create a new code execution sandbox.

        Args:
            name: User-friendly sandbox name (unique per user)
            ttl_minutes: Idle timeout in minutes (default: 10)
            provider: Sandbox provider ("docker", "e2b", etc.). If None, auto-selects.
            template_id: Provider template ID (optional)
            context: Operation context with user/agent/zone info

        Returns:
            Sandbox metadata dict with sandbox_id, name, status, etc.
        """
        ctx = _parse_context(context)

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
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
        as_script: bool = False,
    ) -> dict:
        """Run code in a sandbox.

        Args:
            sandbox_id: Sandbox ID
            language: Programming language ("python", "javascript", "bash")
            code: Code to execute
            timeout: Execution timeout in seconds (default: 300)
            nexus_url: Nexus server URL (auto-injected as env var if provided)
            nexus_api_key: Nexus API key (auto-injected as env var if provided)
            context: Operation context (used to get api_key if nexus_api_key not provided)
            as_script: If True, run as standalone script (stateless).

        Returns:
            Dict with stdout, stderr, exit_code, execution_time
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        # Get Nexus credentials from context if not provided
        if not nexus_api_key and context:
            ctx = _parse_context(context)
            nexus_api_key = getattr(ctx, "api_key", None)

        # Auto-detect nexus_url if not provided
        if not nexus_url:
            nexus_url = os.getenv("NEXUS_SERVER_URL") or os.getenv("NEXUS_URL")

        # Inject Nexus credentials as environment variables in the code
        if nexus_url or nexus_api_key:
            env_prefix = ""
            if language == "bash":
                if nexus_url:
                    env_prefix += f'export NEXUS_URL="{nexus_url}"\n'
                if nexus_api_key:
                    env_prefix += f'export NEXUS_API_KEY="{nexus_api_key}"\n'
                code = env_prefix + code
            elif language == "python":
                env_lines = ["import os"]
                if nexus_url:
                    env_lines.append(f'os.environ["NEXUS_URL"] = "{nexus_url}"')
                if nexus_api_key:
                    env_lines.append(f'os.environ["NEXUS_API_KEY"] = "{nexus_api_key}"')
                env_prefix = "\n".join(env_lines) + "\n"
                code = env_prefix + code
            elif language in ("javascript", "js"):
                env_lines = []
                if nexus_url:
                    env_lines.append(f'process.env.NEXUS_URL = "{nexus_url}";')
                if nexus_api_key:
                    env_lines.append(f'process.env.NEXUS_API_KEY = "{nexus_api_key}";')
                env_prefix = "\n".join(env_lines) + "\n"
                code = env_prefix + code

        execution_result = await self._sandbox_manager.run_code(
            sandbox_id, language, code, timeout, as_script=as_script
        )
        result = dataclasses.asdict(execution_result)
        # Convert ValidationResult Pydantic models to dicts for serialization
        if result.get("validations"):
            result["validations"] = [
                v.model_dump() if hasattr(v, "model_dump") else v for v in result["validations"]
            ]
        return result

    @rpc_expose(description="Validate code in sandbox")
    async def sandbox_validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Run validation pipeline in sandbox.

        Args:
            sandbox_id: Sandbox ID
            workspace_path: Workspace root path in sandbox
            context: Operation context

        Returns:
            Dict with validations list
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        results = await self._sandbox_manager.validate(sandbox_id, workspace_path)
        return {"validations": results}

    @rpc_expose(description="Pause sandbox")
    async def sandbox_pause(
        self,
        sandbox_id: str,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Pause sandbox to save costs.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.pause_sandbox(sandbox_id)
        return result

    @rpc_expose(description="Resume paused sandbox")
    async def sandbox_resume(
        self,
        sandbox_id: str,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Resume a paused sandbox.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.resume_sandbox(sandbox_id)
        return result

    @rpc_expose(description="Stop and destroy sandbox")
    async def sandbox_stop(
        self,
        sandbox_id: str,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.stop_sandbox(sandbox_id)
        return result

    @rpc_expose(description="List sandboxes")
    async def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        """List user's sandboxes.

        Args:
            context: Operation context
            verify_status: If True, verify status with provider (slower but accurate)
            user_id: Filter by user_id (admin only)
            zone_id: Filter by zone_id (admin only)
            agent_id: Filter by agent_id
            status: Filter by status (e.g., 'active', 'stopped', 'paused')

        Returns:
            Dict with list of sandboxes
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        ctx = _parse_context(context)

        # Determine filter values
        # If explicit filter parameters are provided and user is admin, use them
        # Otherwise filter by authenticated user
        filter_user_id = user_id if (user_id is not None and ctx.is_admin) else ctx.user_id
        filter_zone_id = zone_id if (zone_id is not None and ctx.is_admin) else ctx.zone_id
        filter_agent_id = agent_id if agent_id is not None else ctx.agent_id

        sandboxes = await self._sandbox_manager.list_sandboxes(
            user_id=filter_user_id,
            zone_id=filter_zone_id,
            agent_id=filter_agent_id,
            status=status,
            verify_status=verify_status,
        )
        return {"sandboxes": sandboxes}

    @rpc_expose(description="Get sandbox status")
    async def sandbox_status(
        self,
        sandbox_id: str,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.get_sandbox_status(sandbox_id)
        return result

    @rpc_expose(description="Get or create sandbox")
    async def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict:
        """Get existing active sandbox or create a new one.

        Args:
            name: Sandbox name (e.g., "user_id,agent_id")
            ttl_minutes: Idle timeout in minutes (default: 10)
            provider: Sandbox provider ("docker", "e2b", etc.)
            template_id: Provider template ID (optional)
            verify_status: If True, verify with provider that sandbox is running
            context: Operation context with user/agent/zone info

        Returns:
            Sandbox metadata dict (either existing or newly created)
        """
        ctx = _parse_context(context)

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
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Connect and mount Nexus to a sandbox (Nexus-managed or user-managed).

        Args:
            sandbox_id: Sandbox ID (Nexus-managed or external)
            provider: Sandbox provider ("e2b", etc.). Default: "e2b"
            sandbox_api_key: Provider API key (optional, only for user-managed sandboxes)
            mount_path: Path where Nexus will be mounted in sandbox (default: /mnt/nexus)
            nexus_url: Nexus server URL (auto-detected if not provided)
            nexus_api_key: Nexus API key (from context if not provided)
            agent_id: Agent ID for version attribution (issue #418).
            context: Operation context

        Returns:
            Dict with connection details

        Raises:
            ValueError: If provider not supported or required credentials missing
            RuntimeError: If connection/mount fails
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        # Get Nexus URL - should be provided by client
        # Falls back to localhost only for direct server-side calls
        if not nexus_url:
            nexus_url = os.getenv("NEXUS_SERVER_URL") or os.getenv(
                "NEXUS_URL", "http://localhost:2026"
            )

        # Get Nexus API key from context if not provided
        if not nexus_api_key:
            ctx = _parse_context(context)
            nexus_api_key = getattr(ctx, "api_key", None)

        if not nexus_api_key:
            raise ValueError(
                "Nexus API key required for mounting. Pass nexus_api_key or provide in context."
            )

        result: dict[Any, Any] = await self._sandbox_manager.connect_sandbox(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            mount_path=mount_path,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            agent_id=agent_id,
        )
        return result

    @rpc_expose(description="Disconnect from user-managed sandbox")
    async def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Disconnect and unmount Nexus from a user-managed sandbox.

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider ("e2b", etc.). Default: "e2b"
            sandbox_api_key: Provider API key for authentication
            context: Operation context

        Returns:
            Dict with disconnection details

        Raises:
            ValueError: If provider not supported or API key missing
            RuntimeError: If disconnection/unmount fails
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.disconnect_sandbox(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
        )
        return result


def create_sandbox_service(nx: Any) -> SandboxService | None:
    """Create SandboxService from NexusFS kernel params.

    Args:
        nx: NexusFS instance to extract params from.

    Returns:
        SandboxService instance, or None if creation fails.
    """
    try:
        default_ctx = getattr(nx, "_default_context", None)
        if default_ctx is None:
            default_ctx = OperationContext(user_id="system", groups=[])

        return SandboxService(
            session_factory=nx.SessionLocal,
            default_context=default_ctx,
            config=getattr(nx, "_config", None),
        )
    except Exception as exc:
        logger.debug("Failed to create SandboxService: %s", exc)
        return None
