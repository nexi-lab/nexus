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

from nexus.sandbox.sandbox_provider import (
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

# Default E2B template with Nexus + FUSE pre-installed
# Built from e2b-template/e2b.Dockerfile
NEXUS_FUSE_TEMPLATE = "nexus-fuse"

# Lazy import e2b_code_interpreter to avoid import errors if not installed
# We use e2b_code_interpreter instead of base e2b for stateful Python execution
# via Jupyter kernel (variables persist between run_code calls)
try:
    # CommandExitException is still in the base e2b package
    from e2b.sandbox.commands.command_handle import CommandExitException
    from e2b_code_interpreter import AsyncSandbox

    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False
    CommandExitException = Exception  # Fallback for type hints
    logger.warning(
        "e2b_code_interpreter package not installed. E2BSandboxProvider will not work. "
        "Install with: pip install e2b-code-interpreter"
    )


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
            raise RuntimeError(
                "e2b_code_interpreter package not installed. "
                "Install with: pip install e2b-code-interpreter"
            )

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
            # Only pass template if it's not None (E2B will use default if not provided)
            create_kwargs = {
                "api_key": self.api_key,
                "timeout": timeout_minutes * 60,  # E2B uses seconds
                "metadata": metadata or {},
            }
            if template:
                create_kwargs["template"] = template

            sandbox = await AsyncSandbox.create(**create_kwargs)

            # Don't cache - avoid event loop issues (sandbox will reconnect when needed)
            sandbox_id = str(sandbox.sandbox_id)

            template_info = f"template={template}" if template else "default template"
            logger.info(f"Created E2B sandbox: {sandbox_id} ({template_info})")
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
        as_script: bool = False,
    ) -> CodeExecutionResult:
        """Run code in E2B sandbox.

        For Python (as_script=False): Uses Jupyter kernel via e2b_code_interpreter
        for STATEFUL execution - variables persist between calls.

        For Python (as_script=True): Writes to file and runs via shell (stateless).

        For bash/js: Uses shell commands (always stateless).

        Args:
            sandbox_id: E2B sandbox ID
            language: Programming language
            code: Code to execute
            timeout: Execution timeout in seconds
            as_script: If True, write code to file and execute as standalone script
                      (stateless). If False (default), use Jupyter kernel (stateful).

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
        runtime = self.SUPPORTED_LANGUAGES[language]

        # For Python with as_script=False, use Jupyter kernel for stateful execution
        if runtime == "python3" and not as_script:
            return await self._run_python_code(sandbox, sandbox_id, code, timeout)

        # For bash/js or as_script=True, use shell commands (stateless)
        return await self._run_shell_code(sandbox, sandbox_id, language, runtime, code, timeout)

    async def _run_python_code(
        self,
        sandbox: AsyncSandbox,
        sandbox_id: str,
        code: str,
        timeout: int,
    ) -> CodeExecutionResult:
        """Run Python code using Jupyter kernel (stateful).

        Variables and state persist between calls within the same sandbox.
        """
        try:
            start_time = time.time()

            # Use e2b_code_interpreter's run_code for Jupyter-based execution
            # This maintains state between calls (variables persist!)
            # Pass timeout to E2B SDK (code execution) and request_timeout (HTTP)
            execution = await sandbox.run_code(
                code,
                timeout=timeout,
                request_timeout=timeout + 30,  # HTTP timeout with buffer
            )

            execution_time = time.time() - start_time

            # Convert Execution result to CodeExecutionResult format
            # execution.logs contains stdout/stderr lists
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []

            if execution.logs:
                stdout_parts.extend(execution.logs.stdout or [])
                stderr_parts.extend(execution.logs.stderr or [])

            # Add results (text output, repr of return values)
            if execution.results:
                for result in execution.results:
                    if hasattr(result, "text") and result.text:
                        stdout_parts.append(result.text)

            # Handle errors
            exit_code = 0
            if execution.error:
                exit_code = 1
                error_msg = f"{execution.error.name}: {execution.error.value}"
                if execution.error.traceback:
                    error_msg = execution.error.traceback
                stderr_parts.append(error_msg)

            stdout = "\n".join(stdout_parts)
            stderr = "\n".join(stderr_parts)

            logger.debug(
                f"Executed Python code in sandbox {sandbox_id} (Jupyter kernel): "
                f"exit_code={exit_code}, time={execution_time:.2f}s"
            )

            return CodeExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                execution_time=execution_time,
            )

        except TimeoutError as timeout_err:
            logger.warning(f"Python execution timeout in sandbox {sandbox_id}")
            raise ExecutionTimeoutError(
                f"Code execution exceeded {timeout} second timeout"
            ) from timeout_err
        except Exception as e:
            logger.error(f"Python execution failed in sandbox {sandbox_id}: {e}")
            raise

    async def _run_shell_code(
        self,
        sandbox: AsyncSandbox,
        sandbox_id: str,
        language: str,
        runtime: str,
        code: str,
        timeout: int,
    ) -> CodeExecutionResult:
        """Run code using shell commands (stateless).

        Used for bash/js, or Python when as_script=True.
        """
        # Build command based on language
        if runtime == "python3":
            cmd = f"python3 -c {_quote(code)}"
        elif runtime == "node":
            cmd = f"node -e {_quote(code)}"
        elif runtime == "bash":
            cmd = f"bash -c {_quote(code)}"
        else:
            raise UnsupportedLanguageError(f"Unknown runtime: {runtime}")

        try:
            start_time = time.time()

            # Run with timeout using E2B's shell command execution
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
        except CommandExitException as cmd_err:
            # E2B raises CommandExitException for non-zero exit codes
            # This is normal behavior - return the result with the exit code
            execution_time = time.time() - start_time
            logger.debug(
                f"Command exited with non-zero code in sandbox {sandbox_id}: "
                f"exit_code={cmd_err.exit_code}, stderr={cmd_err.stderr}"
            )
            return CodeExecutionResult(
                stdout=cmd_err.stdout or "",
                stderr=cmd_err.stderr or "",
                exit_code=cmd_err.exit_code,
                execution_time=execution_time,
            )
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
            sandbox = await AsyncSandbox.connect(
                sandbox_id, api_key=self.api_key, request_timeout=60
            )
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

    async def prewarm_imports(self, sandbox_id: str) -> None:
        """Pre-warm Python imports in the sandbox.

        This runs heavy nexus module imports in background immediately after
        sandbox creation. By the time mount_nexus() is called, the bytecode
        cache (.pyc files) should be populated, reducing mount time.

        Args:
            sandbox_id: Sandbox ID
        """
        try:
            sandbox = await self._get_sandbox(sandbox_id)
            # Run imports in background - don't wait for completion
            prewarm_cmd = (
                "python3 -c 'from nexus.remote import RemoteNexusFS; "
                "from nexus.fuse.mount import NexusFUSE' > /dev/null 2>&1 &"
            )
            await sandbox.commands.run(prewarm_cmd)
            logger.info(f"Started pre-warm for sandbox {sandbox_id}")
        except Exception as e:
            # Non-fatal - mount will still work, just slower
            logger.debug(f"Pre-warm failed for {sandbox_id}: {e}")

    async def mount_nexus(
        self,
        sandbox_id: str,
        mount_path: str,
        nexus_url: str,
        api_key: str,
        agent_id: str | None = None,
        skip_dependency_checks: bool = False,
    ) -> dict[str, Any]:
        """Mount Nexus filesystem inside E2B sandbox via FUSE.

        Args:
            sandbox_id: E2B sandbox ID
            mount_path: Path where to mount Nexus (e.g., /home/user/nexus)
            nexus_url: Nexus server URL
            api_key: Nexus API key for authentication
            agent_id: Optional agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            skip_dependency_checks: If True, skip nexus/fusepy installation checks.
                Use this for templates with pre-installed dependencies (e.g., nexus-sandbox)
                to reduce mount time by ~10 seconds.

        Returns:
            Mount status dict with success, mount_path, message, files_visible

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            RuntimeError: If mount fails
        """
        sandbox = await self._get_sandbox(sandbox_id)

        logger.info(f"Mounting Nexus at {mount_path} in sandbox {sandbox_id}")

        # Create mount directory
        mkdir_result = await sandbox.commands.run(f"sudo mkdir -p {mount_path}", timeout=30)
        if mkdir_result.exit_code != 0:
            error_msg = f"Failed to create mount directory: {mkdir_result.stderr}"
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Dependency checks and installation
        # Skip these checks for templates with pre-installed dependencies (saves ~10-15s)

        # Check for Rust FUSE binary first (fastest option)
        rust_fuse_available = False
        try:
            check_result = await sandbox.commands.run("which nexus-fuse", timeout=5)
            if check_result.exit_code == 0:
                rust_fuse_available = True
                logger.info("nexus-fuse (Rust) binary found - using fast native FUSE")
        except CommandExitException:
            pass

        if skip_dependency_checks:
            logger.info("Skipping dependency checks (pre-installed template)")
        elif not rust_fuse_available:
            # No Rust binary, fall back to Python FUSE
            # Check if Python nexus module is available (not just 'nexus' binary)
            python_nexus_installed = False
            try:
                check_result = await sandbox.commands.run(
                    "python3 -c 'from nexus.remote import RemoteNexusFS; print(\"ok\")'", timeout=10
                )
                if check_result.exit_code == 0:
                    python_nexus_installed = True
                    logger.info("Python nexus module already available")
            except CommandExitException:
                pass

            # Check and install libfuse (system library required for FUSE)
            libfuse_installed = False
            try:
                check_libfuse = await sandbox.commands.run(
                    "dpkg -l | grep -q fuse || ls /lib/*/libfuse* >/dev/null 2>&1",
                    timeout=10,
                )
                if check_libfuse.exit_code == 0:
                    libfuse_installed = True
                    logger.info("libfuse already installed")
            except CommandExitException:
                pass

            if not libfuse_installed:
                logger.info("Installing libfuse (system FUSE library)...")
                try:
                    # Install libfuse via apt (Ubuntu/Debian)
                    install_result = await sandbox.commands.run(
                        "sudo apt-get update -qq && sudo apt-get install -y -qq fuse libfuse-dev",
                        timeout=120,
                    )
                    logger.info("Successfully installed libfuse")
                except CommandExitException as e:
                    logger.warning(f"Failed to install libfuse via apt: {e.stderr}")

            if not python_nexus_installed:
                # Python nexus not found, try to install
                # Use longer timeout (180s) for pip install as it can take time
                # Install with fuse extra for FUSE mount support
                logger.info("Python nexus module not found, installing nexus-ai-fs[fuse]...")
                try:
                    install_result = await sandbox.commands.run(
                        "pip install -q 'nexus-ai-fs[fuse]'",
                        timeout=180,  # 3 minutes for installation
                    )
                    if install_result.exit_code != 0:
                        error_msg = f"Failed to install nexus-ai-fs: {install_result.stderr}"
                        logger.error(error_msg)
                        return {
                            "success": False,
                            "mount_path": mount_path,
                            "message": error_msg,
                            "files_visible": 0,
                        }
                    logger.info("Successfully installed nexus-ai-fs[fuse]")
                except CommandExitException as e:
                    error_msg = f"Failed to install nexus-ai-fs: {e.stderr}"
                    logger.error(error_msg)
                    return {
                        "success": False,
                        "mount_path": mount_path,
                        "message": error_msg,
                        "files_visible": 0,
                    }
            else:
                # Python nexus is installed, check if fusepy is also installed
                fusepy_installed = False
                try:
                    check_fuse = await sandbox.commands.run(
                        "python3 -c 'import fuse; print(\"ok\")'", timeout=10
                    )
                    if check_fuse.exit_code == 0:
                        fusepy_installed = True
                        logger.info("fusepy already installed")
                except CommandExitException:
                    pass

                if not fusepy_installed:
                    logger.info("fusepy not found, installing...")
                    try:
                        install_result = await sandbox.commands.run(
                            "pip install -q fusepy",
                            timeout=60,
                        )
                        logger.info("Successfully installed fusepy")
                    except CommandExitException as e:
                        logger.warning(f"Failed to install fusepy: {e.stderr}")

        # Mount the filesystem
        logger.info(
            f"Mounting with nexus_url={nexus_url}, api_key={'***' + api_key[-10:] if api_key else 'None'}"
            + (f", agent_id={agent_id}" if agent_id else "")
        )

        if rust_fuse_available:
            # Use Rust FUSE binary (fastest option - ~600ms mount time)
            logger.info("Using nexus-fuse (Rust) for fast native FUSE mount")
            mount_cmd = (
                f"nohup sudo nexus-fuse mount "
                f"--url {nexus_url} "
                f"--api-key {api_key} "
                f"--allow-other "
                f"{mount_path} > /tmp/nexus-mount.log 2>&1 &"
            )
            logger.debug(
                f"Mount command: nexus-fuse mount --url ... --api-key *** --allow-other {mount_path}"
            )
            max_wait = 5  # Rust mount is fast (~600ms)
        else:
            # Fall back to Python FUSE mount
            # Build Python mount script using direct imports
            # NOTE: Import time is ~9s due to nexus/__init__.py importing heavy modules
            logger.info("Using Python FUSE mount (slower fallback)")
            mount_script = f'''
import os, sys, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("nexus-mount")
log.info("Starting direct Python mount...")
try:
    from nexus.remote import RemoteNexusFS
    from nexus.fuse.mount import NexusFUSE, MountMode
    log.info("Imports complete, connecting to server...")
    nx = RemoteNexusFS("{nexus_url}", api_key="{api_key}")
    {"nx.agent_id = '" + agent_id + "'" if agent_id else ""}
    log.info("Creating FUSE mount...")
    fuse = NexusFUSE(nx, "{mount_path}", mode=MountMode.SMART)
    log.info("Starting FUSE (foreground)...")
    fuse.mount(foreground=True, allow_other=True)
except Exception as e:
    log.error(f"Mount failed: {{e}}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
'''
            # Write script to file and run in background with nohup
            script_path = "/tmp/nexus_mount_script.py"
            await sandbox.commands.run(
                f"cat > {script_path} << 'NEXUS_MOUNT_EOF'\n{mount_script}\nNEXUS_MOUNT_EOF",
                timeout=30,
            )
            mount_cmd = f"nohup sudo python3 {script_path} > /tmp/nexus-mount.log 2>&1 &"
            logger.debug(f"Mount command: {mount_cmd}")
            max_wait = 10  # Python mount is slower due to imports

        # Run mount in background
        mount_result = await sandbox.commands.run(mount_cmd, timeout=30)
        if mount_result.exit_code != 0:
            error_msg = f"Failed to start mount: {mount_result.stderr}"
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Wait for mount to initialize
        logger.info("Waiting for FUSE mount to initialize...")
        poll_interval = 0.2  # Reduced from 0.5s for faster detection
        mount_verified = False

        for attempt in range(int(max_wait / poll_interval)):
            # Check if FUSE mount is actually present in mount table
            mount_check_cmd = f"mount | grep -q '{mount_path}'"
            try:
                mount_result = await sandbox.commands.run(mount_check_cmd, timeout=3)
                if mount_result.exit_code == 0:
                    mount_verified = True
                    elapsed = (attempt + 1) * poll_interval
                    logger.info(f"FUSE mount verified after {elapsed:.1f}s")
                    break
            except CommandExitException:
                pass

            await asyncio.sleep(poll_interval)

        if not mount_verified:
            # Polling timed out, check logs for details
            log_stdout = ""
            ps_stdout = ""
            try:
                log_result = await sandbox.commands.run("cat /tmp/nexus-mount.log 2>&1", timeout=10)
                log_stdout = log_result.stdout
            except CommandExitException as e:
                log_stdout = e.stdout or ""
            try:
                ps_result = await sandbox.commands.run(
                    "ps aux | grep -E 'nexus|fuse|python' | grep -v grep",
                    timeout=10,
                )
                ps_stdout = ps_result.stdout
            except CommandExitException as e:
                ps_stdout = e.stdout or ""

            error_msg = (
                f"FUSE mount not found in mount table after {max_wait}s. "
                f"Mount log: {log_stdout}. Processes: {ps_stdout}"
            )
            logger.error(error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Mount verified - small delay for FUSE to stabilize
        await asyncio.sleep(0.3)

        # Verify files are accessible
        try:
            ls_result = await sandbox.commands.run(f"ls {mount_path}/ 2>&1", timeout=10)
            logger.info(f"Successfully mounted Nexus at {mount_path} (verified with mount + ls)")
            return {
                "success": True,
                "mount_path": mount_path,
                "message": f"Nexus mounted successfully at {mount_path}",
                "files_visible": len(ls_result.stdout.strip().split("\n"))
                if ls_result.stdout.strip()
                else 0,
            }
        except CommandExitException as e:
            logger.warning(
                f"FUSE mount present but ls failed: {e.stderr}. Mount may still be initializing."
            )
            return {
                "success": True,
                "mount_path": mount_path,
                "message": f"Nexus mounted at {mount_path} (FUSE present, ls pending)",
                "files_visible": -1,
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
            # Pass request_timeout to avoid "context deadline exceeded" errors
            sandbox = await AsyncSandbox.connect(
                sandbox_id,
                api_key=self.api_key,
                request_timeout=60,  # 60s for reconnection
            )
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
