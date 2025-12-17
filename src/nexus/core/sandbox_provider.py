"""Sandbox provider abstraction for code execution environments.

Provides a unified interface for managing sandboxes across different providers
(E2B, Docker, Modal, etc.). Each provider implements create/run/pause/resume/destroy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class CodeExecutionResult:
    """Result from code execution in sandbox."""

    stdout: str
    stderr: str
    exit_code: int
    execution_time: float  # Seconds


@dataclass
class SandboxInfo:
    """Information about a sandbox."""

    sandbox_id: str
    status: str  # "creating", "active", "paused", "stopped", "error"
    created_at: datetime
    provider: str
    template_id: str | None = None
    metadata: dict[str, Any] | None = None


class SandboxProvider(ABC):
    """Abstract base class for sandbox providers.

    Implementations provide concrete sandbox management for different
    platforms (E2B, Docker, Modal, etc.).
    """

    @abstractmethod
    async def create(
        self,
        template_id: str | None = None,
        timeout_minutes: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new sandbox.

        Args:
            template_id: Template ID for pre-configured environment
            timeout_minutes: Timeout for sandbox creation
            metadata: Provider-specific metadata

        Returns:
            Sandbox ID

        Raises:
            SandboxCreationError: If sandbox creation fails
        """
        ...

    @abstractmethod
    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
    ) -> CodeExecutionResult:
        """Run code in sandbox.

        Args:
            sandbox_id: Sandbox ID
            language: Programming language ("python", "javascript", "bash")
            code: Code to execute
            timeout: Execution timeout in seconds

        Returns:
            Execution result with stdout/stderr/exit_code

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            ExecutionTimeoutError: If execution exceeds timeout
            UnsupportedLanguageError: If language not supported
        """
        ...

    @abstractmethod
    async def pause(self, sandbox_id: str) -> None:
        """Pause sandbox (if supported).

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            UnsupportedOperationError: If provider doesn't support pause
        """
        ...

    @abstractmethod
    async def resume(self, sandbox_id: str) -> None:
        """Resume paused sandbox (if supported).

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            UnsupportedOperationError: If provider doesn't support resume
        """
        ...

    @abstractmethod
    async def destroy(self, sandbox_id: str) -> None:
        """Destroy sandbox and clean up resources.

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        ...

    @abstractmethod
    async def get_info(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox information.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox information

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if provider is available and healthy.

        Returns:
            True if provider is healthy
        """
        ...

    @abstractmethod
    async def mount_nexus(
        self,
        sandbox_id: str,
        mount_path: str,
        nexus_url: str,
        api_key: str,
        agent_id: str | None = None,
        skip_dependency_checks: bool = False,
    ) -> dict[str, Any]:
        """Mount Nexus filesystem inside sandbox via FUSE.

        Args:
            sandbox_id: The sandbox ID
            mount_path: Path inside sandbox where to mount
            nexus_url: Nexus server URL
            api_key: API key for authentication
            agent_id: Optional agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            skip_dependency_checks: If True, skip nexus/fusepy installation checks.
                Use for templates with pre-installed dependencies to save ~10s.

        Returns:
            Mount status dict with:
            - success: bool
            - mount_path: str
            - message: str
            - files_visible: int (number of files/dirs in mount)

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            RuntimeError: If mount operation fails
        """
        ...


class SandboxProviderError(Exception):
    """Base exception for sandbox provider errors."""

    pass


class SandboxCreationError(SandboxProviderError):
    """Raised when sandbox creation fails."""

    pass


class SandboxNotFoundError(SandboxProviderError):
    """Raised when sandbox doesn't exist."""

    pass


class ExecutionTimeoutError(SandboxProviderError):
    """Raised when code execution times out."""

    pass


class UnsupportedLanguageError(SandboxProviderError):
    """Raised when language is not supported."""

    pass


class UnsupportedOperationError(SandboxProviderError):
    """Raised when operation is not supported by provider."""

    pass
