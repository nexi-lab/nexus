"""E2B sandbox provider implementation.

Implements SandboxProvider interface using E2B (https://e2b.dev) as the backend.
E2B provides cloud-based code execution sandboxes with fast startup times.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from nexus.core.sandbox_provider import (
    CodeExecutionResult,
    ExecutionTimeoutError,
    SandboxCreationError,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
    UnsupportedLanguageError,
    UnsupportedOperationError,
)

logger = logging.getLogger(__name__)

# Lazy import e2b to avoid import errors if not installed
try:
    from e2b import AsyncSandbox

    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False
    logger.warning("e2b package not installed. E2BSandboxProvider will not work.")


class E2BSandboxProvider(SandboxProvider):
    """E2B sandbox provider implementation.

    Uses E2B SDK to manage sandboxes for code execution.
    """

    # Supported languages mapping to E2B runtime
    SUPPORTED_LANGUAGES = {
        "python": "python3",
        "javascript": "node",
        "js": "node",
        "bash": "bash",
        "sh": "bash",
    }

    def __init__(
        self,
        api_key: str | None = None,
        team_id: str | None = None,
        default_template: str | None = None,
    ):
        """Initialize E2B provider.

        Args:
            api_key: E2B API key (defaults to E2B_API_KEY env var)
            team_id: E2B team ID (optional)
            default_template: Default template ID for sandboxes
        """
        if not E2B_AVAILABLE:
            raise RuntimeError("e2b package not installed. Install with: pip install e2b")

        self.api_key = api_key or os.getenv("E2B_API_KEY")
        if not self.api_key:
            raise ValueError(
                "E2B API key required. Set E2B_API_KEY env var or pass api_key parameter."
            )

        self.team_id = team_id
        self.default_template = default_template

        # Cache for active sandboxes
        self._sandboxes: dict[str, AsyncSandbox] = {}

    async def create(
        self,
        template_id: str | None = None,
        timeout_minutes: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new E2B sandbox.

        Args:
            template_id: E2B template ID (uses default if not provided)
            timeout_minutes: Sandbox timeout (E2B default)
            metadata: Additional metadata (stored but not used by E2B)

        Returns:
            Sandbox ID

        Raises:
            SandboxCreationError: If sandbox creation fails
        """
        try:
            # Use provided template or default
            template = template_id or self.default_template

            # Create async sandbox using E2B's native async API
            sandbox = await AsyncSandbox.create(
                template=template,
                api_key=self.api_key,
                timeout=timeout_minutes * 60,  # E2B uses seconds
                metadata=metadata or {},
            )

            # Cache sandbox instance
            sandbox_id = str(sandbox.sandbox_id)
            self._sandboxes[sandbox_id] = sandbox

            logger.info(f"Created E2B sandbox: {sandbox_id} (template={template})")
            return sandbox_id

        except Exception as e:
            logger.error(f"Failed to create E2B sandbox: {e}")
            raise SandboxCreationError(f"E2B sandbox creation failed: {e}") from e

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 30,
    ) -> CodeExecutionResult:
        """Run code in E2B sandbox.

        Args:
            sandbox_id: E2B sandbox ID
            language: Programming language
            code: Code to execute
            timeout: Execution timeout in seconds

        Returns:
            Execution result

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            ExecutionTimeoutError: If execution times out
            UnsupportedLanguageError: If language not supported
        """
        # Validate language
        if language not in self.SUPPORTED_LANGUAGES:
            supported = ", ".join(self.SUPPORTED_LANGUAGES.keys())
            raise UnsupportedLanguageError(
                f"Language '{language}' not supported. Supported: {supported}"
            )

        # Get sandbox
        sandbox = await self._get_sandbox(sandbox_id)

        # Build command based on language
        runtime = self.SUPPORTED_LANGUAGES[language]
        if runtime == "python3":
            cmd = f"python3 -c {_quote(code)}"
        elif runtime == "node":
            cmd = f"node -e {_quote(code)}"
        elif runtime == "bash":
            cmd = f"bash -c {_quote(code)}"
        else:
            raise UnsupportedLanguageError(f"Unknown runtime: {runtime}")

        # Execute code using E2B's async API
        try:
            start_time = time.time()

            # Run with timeout using E2B's native async command execution
            result = await asyncio.wait_for(
                sandbox.commands.run(cmd),
                timeout=timeout,
            )

            execution_time = time.time() - start_time

            logger.debug(
                f"Executed {language} code in sandbox {sandbox_id}: "
                f"exit_code={result.exit_code}, time={execution_time:.2f}s"
            )

            return CodeExecutionResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                execution_time=execution_time,
            )

        except TimeoutError as timeout_err:
            logger.warning(f"Code execution timeout in sandbox {sandbox_id}")
            raise ExecutionTimeoutError(
                f"Code execution exceeded {timeout} second timeout"
            ) from timeout_err
        except Exception as e:
            logger.error(f"Code execution failed in sandbox {sandbox_id}: {e}")
            raise

    async def pause(self, sandbox_id: str) -> None:  # noqa: ARG002
        """Pause E2B sandbox.

        Note: E2B doesn't support pause/resume. This is a no-op.

        Args:
            sandbox_id: Sandbox ID (unused - required for interface)

        Raises:
            UnsupportedOperationError: Always (E2B doesn't support pause)
        """
        raise UnsupportedOperationError(
            "E2B doesn't support pause/resume. Use stop to destroy the sandbox."
        )

    async def resume(self, sandbox_id: str) -> None:  # noqa: ARG002
        """Resume E2B sandbox.

        Note: E2B doesn't support pause/resume. This is a no-op.

        Args:
            sandbox_id: Sandbox ID (unused - required for interface)

        Raises:
            UnsupportedOperationError: Always (E2B doesn't support resume)
        """
        raise UnsupportedOperationError(
            "E2B doesn't support pause/resume. Create a new sandbox instead."
        )

    async def destroy(self, sandbox_id: str) -> None:
        """Destroy E2B sandbox.

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        sandbox = self._sandboxes.pop(sandbox_id, None)
        if not sandbox:
            logger.warning(f"Sandbox {sandbox_id} not in cache, cannot destroy")
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        try:
            await sandbox.close()
            logger.info(f"Destroyed E2B sandbox: {sandbox_id}")
        except Exception as e:
            logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")
            raise

    async def get_info(self, sandbox_id: str) -> SandboxInfo:
        """Get E2B sandbox information.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox information

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        sandbox = await self._get_sandbox(sandbox_id)

        # E2B doesn't expose much metadata, so we infer status
        status = "active"  # If we can get it, it's active

        return SandboxInfo(
            sandbox_id=sandbox_id,
            status=status,
            created_at=datetime.now(UTC),  # E2B doesn't provide creation time
            provider="e2b",
            template_id=getattr(sandbox, "template", None),
            metadata=getattr(sandbox, "metadata", None),
        )

    async def is_available(self) -> bool:
        """Check if E2B provider is available.

        Returns:
            True if E2B SDK is available and API key is set
        """
        return E2B_AVAILABLE and bool(self.api_key)

    async def _get_sandbox(self, sandbox_id: str) -> AsyncSandbox:
        """Get sandbox from cache or reconnect.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox instance

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        # Try cache first
        if sandbox_id in self._sandboxes:
            return self._sandboxes[sandbox_id]

        # Try to connect to existing sandbox
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self.api_key)
            self._sandboxes[sandbox_id] = sandbox
            return sandbox
        except Exception as e:
            logger.error(f"Failed to connect to sandbox {sandbox_id}: {e}")
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found") from e


def _quote(s: str) -> str:
    """Quote string for shell execution.

    Args:
        s: String to quote

    Returns:
        Quoted string safe for shell
    """
    # Use single quotes and escape any single quotes in the string
    return "'" + s.replace("'", "'\\''") + "'"
