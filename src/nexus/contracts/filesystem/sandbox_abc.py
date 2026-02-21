"""Sandbox sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern.

Contains: sandbox_available property + 11 sandbox methods
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SandboxABC(ABC):
    """Sandbox execution operations."""

    @property
    def sandbox_available(self) -> bool:
        """Whether sandbox execution is available.

        Returns True if at least one sandbox provider is configured.
        Subclasses should override this to check their sandbox manager.
        """
        return False

    @abstractmethod
    def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = "e2b",
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Create a new code execution sandbox.

        Args:
            name: User-friendly sandbox name
            ttl_minutes: Idle timeout in minutes
            provider: Sandbox provider ("e2b", "docker", etc.)
            template_id: Provider template ID
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        ...

    @abstractmethod
    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Get existing active sandbox or create a new one.

        Args:
            name: Sandbox name
            ttl_minutes: Idle timeout in minutes
            provider: Sandbox provider
            template_id: Provider template ID
            verify_status: Whether to verify the sandbox status
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        ...

    @abstractmethod
    def sandbox_run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
        as_script: bool = False,
    ) -> dict[Any, Any]:
        """Run code in a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            language: Programming language
            code: Code to execute
            timeout: Execution timeout in seconds
            nexus_url: Nexus server URL for credential injection
            nexus_api_key: Nexus API key for credential injection
            context: Operation context
            as_script: If True, run as standalone script (stateless)

        Returns:
            Execution result dict
        """
        ...

    @abstractmethod
    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Pause a running sandbox."""
        ...

    @abstractmethod
    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Resume a paused sandbox."""
        ...

    @abstractmethod
    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Stop a sandbox."""
        ...

    @abstractmethod
    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        """List all sandboxes for the current user.

        Args:
            context: Operation context
            verify_status: Whether to verify sandbox status
            user_id: Filter by user ID
            zone_id: Filter by zone ID
            agent_id: Filter by agent ID
            status: Filter by status

        Returns:
            List of sandbox metadata dicts
        """
        ...

    @abstractmethod
    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Get sandbox status."""
        ...

    @abstractmethod
    def sandbox_connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Connect to user-managed sandbox.

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider
            sandbox_api_key: Provider API key
            mount_path: Mount path in sandbox
            nexus_url: Nexus server URL for mounting
            nexus_api_key: Nexus API key for mounting
            agent_id: Agent ID for version attribution
            context: Operation context

        Returns:
            Connection result dict
        """
        ...

    @abstractmethod
    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Disconnect from user-managed sandbox.

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider
            sandbox_api_key: Provider API key
            context: Operation context

        Returns:
            Disconnection result dict
        """
        ...
