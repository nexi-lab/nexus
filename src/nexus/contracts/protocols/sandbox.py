"""Sandbox service protocol (Issue #2051: Decompose SandboxManager).

Defines the contract for sandbox lifecycle management across providers.
Concrete implementation: ``nexus.bricks.sandbox.sandbox_manager.SandboxManager``.

Storage Affinity: **RecordStore** — sandbox metadata persistence.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3 (Feature Bricks Catalog)
    - Issue #2051: Decompose SandboxManager and DockerSandboxProvider
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SandboxProtocol(Protocol):
    """Service contract for sandbox lifecycle management.

    Manages sandbox creation, code execution, lifecycle operations
    (pause/resume/stop), FUSE mount/unmount, and expired sandbox cleanup.

    Providers (Docker, E2B, Monty) are abstracted behind this protocol.
    Smart routing (monty → docker → e2b) is an internal concern.
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
            name: User-friendly name (unique per user among active sandboxes).
            user_id: Owner user ID.
            zone_id: Zone for multi-zone isolation.
            agent_id: Optional agent ID.
            ttl_minutes: Idle timeout in minutes.
            provider: Provider name ("docker", "e2b", "monty").
                If None, auto-selects best available.
            template_id: Provider template ID.

        Returns:
            Sandbox metadata dict with sandbox_id, name, status, etc.

        Raises:
            ValueError: If provider not available or name already exists.
        """
        ...

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        as_script: bool = False,
    ) -> Any:
        """Run code in a sandbox.

        Handles escalation transparently (monty → docker → e2b).

        Args:
            sandbox_id: Sandbox ID.
            language: Programming language ("python", "javascript", "bash").
            code: Code to execute.
            timeout: Execution timeout in seconds.
            as_script: If True, run as standalone script (stateless).

        Returns:
            Execution result with stdout, stderr, exit_code, execution_time.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        ...

    async def pause_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Pause a sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata dict.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        ...

    async def resume_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Resume a paused sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata dict.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        ...

    async def stop_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Stop and destroy a sandbox.

        Args:
            sandbox_id: Sandbox ID.

        Returns:
            Updated sandbox metadata dict.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
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
            status: Filter by status ("active", "paused", "stopped").
            verify_status: If True, verify status with provider (slower).

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

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
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
            user_id: Owner user ID.
            zone_id: Zone ID.
            agent_id: Optional agent ID.
            ttl_minutes: Idle timeout in minutes.
            provider: Provider name.
            template_id: Provider template ID.
            verify_status: If True, verify existing sandbox with provider.

        Returns:
            Sandbox metadata dict (existing or newly created).

        Raises:
            ValueError: If provider not available.
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
            mount_path: FUSE mount path inside sandbox.
            nexus_url: Nexus server URL.
            nexus_api_key: Nexus API key.
            agent_id: Optional agent ID for version attribution.
            skip_dependency_checks: Skip nexus CLI installation checks.

        Returns:
            Connection result dict.

        Raises:
            ValueError: If provider not available or credentials missing.
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
            sandbox_id: Sandbox ID.
            provider: Provider name.
            sandbox_api_key: Provider API key.

        Returns:
            Disconnection result dict.

        Raises:
            ValueError: If provider not available.
        """
        ...

    async def cleanup_expired_sandboxes(self) -> int:
        """Clean up expired sandboxes.

        Returns:
            Number of sandboxes cleaned up.
        """
        ...

    async def validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
    ) -> list[dict[str, Any]]:
        """Run validation pipeline in a sandbox.

        Args:
            sandbox_id: Sandbox ID.
            workspace_path: Path to workspace root in sandbox.

        Returns:
            List of validation result dicts.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        ...

    # NOTE: set_monty_host_functions() and wire_router() are implementation
    # details on SandboxManager, not part of the service contract.
    # See SandboxManager for these lifecycle/wiring methods.
