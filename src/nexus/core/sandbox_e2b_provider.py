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

            # Don't cache - avoid event loop issues (sandbox will reconnect when needed)
            sandbox_id = str(sandbox.sandbox_id)

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
        timeout: int = 300,
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
        # Reconnect to sandbox before destroying (no caching to avoid event loop issues)
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self.api_key)
        except Exception as e:
            logger.error(f"Failed to connect to sandbox {sandbox_id} for destruction: {e}")
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found") from e

        try:
            await sandbox.kill()
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

    async def mount_nexus(
        self,
        sandbox_id: str,
        mount_path: str,
        nexus_url: str,
        api_key: str,
    ) -> dict[str, Any]:
        """Mount Nexus filesystem inside E2B sandbox via FUSE.

        Args:
            sandbox_id: E2B sandbox ID
            mount_path: Path where to mount Nexus (e.g., /home/user/nexus)
            nexus_url: Nexus server URL
            api_key: Nexus API key for authentication

        Returns:
            Mount status dict with success, mount_path, message, files_visible

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            RuntimeError: If mount fails
        """
        sandbox = await self._get_sandbox(sandbox_id)

        logger.info(f"Mounting Nexus at {mount_path} in sandbox {sandbox_id}")

        # Create mount directory
        mkdir_result = await sandbox.commands.run(f"mkdir -p {mount_path}")
        if mkdir_result.exit_code != 0:
            error_msg = f"Failed to create mount directory: {mkdir_result.stderr}"
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Check if nexus CLI is available
        check_result = await sandbox.commands.run("which nexus")
        if check_result.exit_code != 0:
            # nexus not found, try to install
            logger.info("nexus CLI not found, installing nexus-ai-fs...")
            install_result = await sandbox.commands.run("pip install -q nexus-ai-fs")
            if install_result.exit_code != 0:
                error_msg = f"Failed to install nexus-ai-fs: {install_result.stderr}"
                logger.error(error_msg)
                return {
                    "success": False,
                    "mount_path": mount_path,
                    "message": error_msg,
                    "files_visible": 0,
                }
            logger.info("Successfully installed nexus-ai-fs")
        else:
            logger.info("nexus CLI already available, skipping installation")

        # Build mount command with sudo and allow-other
        # Use nohup to run in background
        logger.info(
            f"Mounting with nexus_url={nexus_url}, api_key={'***' + api_key[-10:] if api_key else 'None'}"
        )
        base_mount = (
            f"sudo NEXUS_API_KEY={api_key} "
            f"nexus mount {mount_path} "
            f"--remote-url {nexus_url} "
            f"--allow-other"
        )
        mount_cmd = f"nohup {base_mount} > /tmp/nexus-mount.log 2>&1 &"
        logger.debug(f"Mount command: {mount_cmd}")

        # Run mount in background
        mount_result = await sandbox.commands.run(mount_cmd)
        if mount_result.exit_code != 0:
            error_msg = f"Failed to start mount: {mount_result.stderr}"
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Wait for mount to initialize (FUSE needs time to mount and authenticate)
        # Initial connection may take longer due to remote API calls
        logger.info("Waiting for FUSE mount to initialize...")
        await asyncio.sleep(5)

        # Verify mount with lightweight stat check (avoids timeout from full directory listing)
        # The first stat may timeout as FUSE initializes, so we retry with exponential backoff
        max_retries = 3
        base_delay = 5  # Start with 5 seconds
        stat_result = None

        for attempt in range(max_retries):
            logger.info(f"Verifying mount (attempt {attempt + 1}/{max_retries})...")
            # Use stat instead of ls - just checks mount accessibility without listing all files
            stat_cmd = f"timeout 10 stat {mount_path}"
            stat_result = await sandbox.commands.run(stat_cmd, timeout=15)

            if stat_result.exit_code == 0:
                # Success! Mount is accessible
                break

            if attempt < max_retries - 1:
                # Exponential backoff: 5s, 10s, 20s
                retry_delay = base_delay * (2**attempt)
                logger.warning(
                    f"Mount verification attempt {attempt + 1} failed, retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
        else:
            # All retries failed, check logs for details
            log_result = await sandbox.commands.run("cat /tmp/nexus-mount.log")
            error_msg = (
                f"Mount verification failed after {max_retries} attempts. Log: {log_result.stdout}"
            )
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Mount verified successfully
        if stat_result and stat_result.exit_code == 0:
            logger.info(f"Successfully mounted Nexus at {mount_path} (verified with stat)")
            return {
                "success": True,
                "mount_path": mount_path,
                "message": f"Nexus mounted successfully at {mount_path}",
                "files_visible": -1,  # Not counted with stat check
            }
        else:
            error_msg = "Mount verification failed: stat check unsuccessful"
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

    async def _get_sandbox(self, sandbox_id: str) -> AsyncSandbox:
        """Get sandbox by reconnecting (no caching to avoid event loop issues).

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Fresh sandbox instance

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        # Always reconnect to avoid event loop issues
        # DO NOT cache - cached sandbox objects have asyncio objects bound to specific event loops
        # Each request may run in a different event loop, so we must reconnect every time
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self.api_key)
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
