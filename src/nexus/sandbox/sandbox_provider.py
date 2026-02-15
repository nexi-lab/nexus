"""Sandbox provider abstraction for code execution environments.

Provides a unified interface for managing sandboxes across different providers
(E2B, Docker, Modal, etc.). Each provider implements create/run/pause/resume/destroy.
"""

from __future__ import annotations

import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.sandbox.security_profile import SandboxSecurityProfile
    from nexus.validation.models import ValidationResult

# Validation patterns for shell-safe inputs
_MOUNT_PATH_PATTERN = re.compile(r"^/[a-zA-Z0-9/_\-.]+$")
_AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_,\-.:@]+$")


def validate_mount_path(path: str) -> str:
    """Validate a mount path is safe for shell interpolation.

    Args:
        path: Mount path (must be absolute, no shell metacharacters).

    Returns:
        The validated path.

    Raises:
        ValueError: If the path contains invalid characters.
    """
    if not _MOUNT_PATH_PATTERN.match(path):
        raise ValueError(
            f"Invalid mount path: {path!r} — must be absolute and contain only "
            "alphanumeric, '/', '_', '-', '.' characters"
        )
    return path


def validate_nexus_url(url: str) -> str:
    """Validate a Nexus server URL is safe for shell interpolation.

    Args:
        url: Nexus server URL (must be http or https).

    Returns:
        The validated URL.

    Raises:
        ValueError: If the URL scheme is invalid or contains shell metacharacters.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {url!r} — must be http or https")
    # Check for shell metacharacters in the full URL
    if any(c in url for c in ";&|`$(){}[]!#~"):
        raise ValueError(f"URL contains shell metacharacters: {url!r}")
    return url


def validate_agent_id(agent_id: str) -> str:
    """Validate an agent ID is safe for shell interpolation.

    Args:
        agent_id: Agent identifier.

    Returns:
        The validated agent ID.

    Raises:
        ValueError: If the agent ID contains invalid characters.
    """
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise ValueError(
            f"Invalid agent_id: {agent_id!r} — must contain only "
            "alphanumeric, '_', ',', '-', '.', ':', '@' characters"
        )
    return agent_id


SUPPORTED_LANGUAGE_KEYS = frozenset({"python", "javascript", "js", "bash", "sh"})


def validate_language(language: str, supported: dict[str, str] | None = None) -> str:
    """Validate a language key is supported.

    Args:
        language: Language key to validate.
        supported: Optional mapping of language keys to runtime commands.
            If None, uses the default SUPPORTED_LANGUAGE_KEYS.

    Returns:
        The validated language key.

    Raises:
        UnsupportedLanguageError: If language is not supported.
    """
    keys = set(supported.keys()) if supported else SUPPORTED_LANGUAGE_KEYS
    if language not in keys:
        raise UnsupportedLanguageError(
            f"Language '{language}' not supported. Supported: {', '.join(sorted(keys))}"
        )
    return language


@dataclass
class CodeExecutionResult:
    """Result from code execution in sandbox."""

    stdout: str
    stderr: str
    exit_code: int
    execution_time: float  # Seconds
    validations: list[ValidationResult] | None = None


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
        security_profile: SandboxSecurityProfile | None = None,
    ) -> str:
        """Create a new sandbox.

        Args:
            template_id: Template ID for pre-configured environment
            timeout_minutes: Timeout for sandbox creation
            metadata: Provider-specific metadata
            security_profile: Security profile for container settings.
                Providers that manage their own isolation (e.g., E2B)
                may ignore this parameter.

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
        as_script: bool = False,
    ) -> CodeExecutionResult:
        """Run code in sandbox.

        Args:
            sandbox_id: Sandbox ID
            language: Programming language ("python", "javascript", "bash")
            code: Code to execute
            timeout: Execution timeout in seconds
            as_script: If True, run code as a script file instead of REPL

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
