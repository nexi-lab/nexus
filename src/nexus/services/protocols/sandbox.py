"""Sandbox service protocol (Issue #988: Extract domain services).

Defines the contract for sandbox lifecycle and code execution operations.
Existing implementation: ``nexus.sandbox.sandbox_manager.SandboxManager``.

Storage Affinity: **RecordStore** — sandbox metadata persisted in relational DB.

References:
    - docs/design/KERNEL-ARCHITECTURE.md
    - ops-scenario-matrix.md §3.1, scenario S14 (Sandbox Execution)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SandboxProtocol(Protocol):
    """Service contract for sandbox lifecycle and code execution.

    Provides operations for managing sandboxes across providers
    (Docker, E2B, Monty, etc.):
    - Create/stop/pause/resume sandboxes
    - Execute code in sandboxes
    - List and query sandbox status
    - Connect/disconnect Nexus mounts inside sandboxes
    - Clean up expired sandboxes
    """

    async def create_sandbox(
        self,
        name: str,
        user_id: str,
        zone_id: str,
        agent_id: str | None = None,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new sandbox.

        Args:
            name: User-friendly name (unique per user).
            user_id: User ID.
            zone_id: Zone ID.
            agent_id: Agent ID (optional).
            ttl_minutes: Idle timeout in minutes.
            provider: Provider name ("docker", "e2b", etc.).
            template_id: Template ID for provider.

        Returns:
            Sandbox metadata dict.
        """
        ...

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        as_script: bool = False,
        auto_validate: bool | None = None,
    ) -> Any:
        """Run code in sandbox.

        Args:
            sandbox_id: Sandbox ID.
            language: Programming language.
            code: Code to execute.
            timeout: Timeout in seconds.
            as_script: If True, run as standalone script.
            auto_validate: If True, run validation after execution.

        Returns:
            CodeExecutionResult with stdout, stderr, exit_code, execution_time.
        """
        ...

    async def validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
    ) -> list[dict[str, Any]]:
        """Run validation pipeline in sandbox.

        Args:
            sandbox_id: Sandbox ID.
            workspace_path: Path to workspace root in sandbox.

        Returns:
            List of validation result dicts.
        """
        ...

    async def pause_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Pause sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata.
        """
        ...

    async def resume_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Resume paused sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata.
        """
        ...

    async def stop_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata.
        """
        ...

    async def list_sandboxes(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        verify_status: bool = False,
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering.

        Args:
            user_id: Filter by user.
            zone_id: Filter by zone.
            agent_id: Filter by agent.
            status: Filter by status.
            verify_status: If True, verify status with provider.

        Returns:
            List of sandbox metadata dicts.
        """
        ...

    async def get_sandbox_status(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Sandbox metadata dict.
        """
        ...

    async def get_or_create_sandbox(
        self,
        name: str,
        user_id: str,
        zone_id: str,
        agent_id: str | None = None,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
    ) -> dict[str, Any]:
        """Get existing active sandbox or create a new one.

        Args:
            name: User-friendly sandbox name.
            user_id: User ID.
            zone_id: Zone ID.
            agent_id: Agent ID (optional).
            ttl_minutes: Idle timeout in minutes.
            provider: Sandbox provider.
            template_id: Provider template ID.
            verify_status: If True, verify status with provider.

        Returns:
            Sandbox metadata dict.
        """
        ...

    async def connect_sandbox(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        skip_dependency_checks: bool | None = None,
    ) -> dict[str, Any]:
        """Connect and mount Nexus to a sandbox.

        Args:
            sandbox_id: Sandbox ID.
            provider: Provider name.
            sandbox_api_key: Provider API key (for user-managed sandboxes).
            mount_path: Path where Nexus will be mounted.
            nexus_url: Nexus server URL.
            nexus_api_key: Nexus API key.
            agent_id: Agent ID for version attribution.
            skip_dependency_checks: Skip nexus/fusepy installation checks.

        Returns:
            Dict with connection details.
        """
        ...

    async def disconnect_sandbox(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
    ) -> dict[str, Any]:
        """Disconnect and unmount Nexus from a sandbox.

        Args:
            sandbox_id: External sandbox ID.
            provider: Provider name.
            sandbox_api_key: Provider API key.

        Returns:
            Dict with disconnection details.
        """
        ...

    async def cleanup_expired_sandboxes(self) -> int:
        """Clean up expired sandboxes.

        Returns:
            Number of sandboxes cleaned up.
        """
        ...
