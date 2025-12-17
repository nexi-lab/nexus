"""Docker sandbox provider implementation.

Implements SandboxProvider interface using Docker containers for local code execution.
Designed for development and testing environments.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.core.sandbox_provider import (
    CodeExecutionResult,
    ExecutionTimeoutError,
    SandboxCreationError,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
    UnsupportedLanguageError,
)

logger = logging.getLogger(__name__)

# Lazy import docker to avoid import errors if not installed
try:
    import docker.errors
    from docker.errors import NotFound

    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    logger.warning("docker package not installed. DockerSandboxProvider will not work.")


@dataclass
class ContainerInfo:
    """Internal container tracking information."""

    container: Any  # docker.models.containers.Container
    sandbox_id: str
    created_at: datetime
    expires_at: datetime
    template_id: str | None
    metadata: dict[str, Any]
    status: str  # "active", "paused", "stopped"


class DockerSandboxProvider(SandboxProvider):
    """Docker-based local sandbox provider.

    Implements SandboxProvider interface using Docker containers for
    local code execution. Designed for development and testing.
    """

    # Supported languages mapping to runtime commands
    SUPPORTED_LANGUAGES = {
        "python": "python",
        "javascript": "node",
        "js": "node",
        "bash": "bash",
        "sh": "bash",
    }

    def __init__(
        self,
        docker_client: Any | None = None,  # docker.DockerClient | None
        default_image: str = "nexus-sandbox:latest",
        cleanup_interval: int = 60,
        auto_pull: bool = False,
        memory_limit: str = "512m",
        cpu_limit: float = 1.0,
        network_name: str | None = None,
        docker_config: Any = None,  # DockerTemplateConfig | None
    ):
        """Initialize Docker sandbox provider.

        Args:
            docker_client: Docker client (defaults to docker.from_env())
            default_image: Default container image (default: nexus-runtime:latest with sudo)
            cleanup_interval: Seconds between cleanup checks
            auto_pull: Auto-pull missing images (disabled by default for custom images)
            memory_limit: Memory limit (e.g., "512m", "1g")
            cpu_limit: CPU limit in cores (e.g., 1.0 = 1 core)
            network_name: Docker network name (defaults to NEXUS_DOCKER_NETWORK env var)
            docker_config: Docker template configuration for custom images
        """
        if not DOCKER_AVAILABLE:
            raise RuntimeError("docker package not installed. Install with: pip install docker")

        # Initialize Docker client with fallback for Colima
        if docker_client:
            self.docker_client = docker_client
        else:
            try:
                # Try default Docker socket
                self.docker_client = docker.from_env()  # type: ignore[attr-defined]
            except Exception as e:
                # Try Colima socket path
                import os

                colima_socket = os.path.expanduser("~/.colima/default/docker.sock")
                if os.path.exists(colima_socket):
                    self.docker_client = docker.DockerClient(base_url=f"unix://{colima_socket}")  # type: ignore[attr-defined]
                else:
                    raise RuntimeError(
                        "Cannot connect to Docker. Make sure Docker is running.\n"
                        "For Colima users: Try 'colima start'"
                    ) from e

        self.default_image = default_image
        self.cleanup_interval = cleanup_interval
        self.auto_pull = auto_pull
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.docker_config = docker_config

        # Initialize network name (optional - only required for Docker Compose)
        import os

        self.network_name = network_name or os.environ.get("NEXUS_DOCKER_NETWORK")
        # Note: network_name is optional - if not set, containers will use default bridge network

        # Cache for active containers
        self._containers: dict[str, ContainerInfo] = {}

        # Cleanup task
        self._cleanup_task: asyncio.Task | None = None
        self._cleanup_running = False

    async def create(
        self,
        template_id: str | None = None,
        timeout_minutes: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new Docker sandbox.

        Args:
            template_id: Docker image to use (defaults to default_image)
            timeout_minutes: Sandbox TTL in minutes
            metadata: Additional metadata

        Returns:
            Sandbox ID (container ID)

        Raises:
            SandboxCreationError: If sandbox creation fails
        """
        try:
            # Resolve template_id to Docker image name
            image = self._resolve_image(template_id)

            # Ensure image exists
            await asyncio.to_thread(self._ensure_image, image)

            # Calculate expiration time
            created_at = datetime.now(UTC)
            expires_at = created_at + timedelta(minutes=timeout_minutes)

            # Extract name from metadata if provided
            container_name = metadata.get("name") if metadata else None

            # Create container with resource limits
            container = await asyncio.to_thread(
                self._create_container,
                image,
                container_name,
            )

            # Generate sandbox ID (use first 12 chars of container ID)
            sandbox_id: str = container.id[:12]

            # Store container info
            self._containers[sandbox_id] = ContainerInfo(
                container=container,
                sandbox_id=sandbox_id,
                created_at=created_at,
                expires_at=expires_at,
                template_id=image,
                metadata=metadata or {},
                status="active",
            )

            # Start cleanup task if not running
            if not self._cleanup_running:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                self._cleanup_running = True

            name_info = f", name={container_name}" if container_name else ""
            logger.info(
                f"Created Docker sandbox: {sandbox_id} (image={image}, ttl={timeout_minutes}m{name_info})"
            )
            return sandbox_id

        except Exception as e:
            logger.error(f"Failed to create Docker sandbox: {e}")
            raise SandboxCreationError(f"Docker sandbox creation failed: {e}") from e

    def _resolve_image(self, template_id: str | None) -> str:
        """Resolve template_id to Docker image name.

        Args:
            template_id: Template name or direct image name

        Returns:
            Docker image name to use
        """
        # No template specified, use default
        if not template_id:
            default_img: str = (
                self.docker_config.default_image if self.docker_config else self.default_image
            )
            return default_img

        # Check if it's a configured template
        if self.docker_config and template_id in self.docker_config.templates:
            template = self.docker_config.templates[template_id]
            # Use the configured image name for this template
            if template.image:
                logger.info(f"Resolved template '{template_id}' to image: {template.image}")
                image_name: str = template.image
                return image_name
            else:
                logger.warning(
                    f"Template '{template_id}' has no image configured, using as literal image name"
                )
                return template_id

        # Treat as direct image name
        logger.debug(f"Using '{template_id}' as direct image name")
        return template_id

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
    ) -> CodeExecutionResult:
        """Run code in Docker sandbox.

        Args:
            sandbox_id: Sandbox ID
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

        # Get container
        container_info = self._get_container_info(sandbox_id)
        container = container_info.container

        # Build execution command
        cmd = self._build_command(language, code)

        # Execute code with timeout
        try:
            start_time = time.time()
            logger.info(
                f"[DOCKER-EXEC] Starting execution in sandbox {sandbox_id}, timeout={timeout}s"
            )
            logger.info(f"[DOCKER-EXEC] Command: {cmd}")

            # Run command in container with timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run,
                    cmd,
                    demux=True,  # Separate stdout and stderr
                ),
                timeout=timeout,
            )

            execution_time = time.time() - start_time
            logger.info(f"[DOCKER-EXEC] Execution completed in {execution_time:.2f}s")

            # Extract stdout and stderr (demux returns tuple)
            stdout_bytes, stderr_bytes = result.output
            stdout = stdout_bytes.decode("utf-8") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8") if stderr_bytes else ""

            logger.debug(
                f"Executed {language} code in sandbox {sandbox_id}: "
                f"exit_code={result.exit_code}, time={execution_time:.2f}s"
            )

            return CodeExecutionResult(
                stdout=stdout,
                stderr=stderr,
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

    async def pause(self, sandbox_id: str) -> None:
        """Pause Docker sandbox.

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        container_info = self._get_container_info(sandbox_id)
        container = container_info.container

        try:
            await asyncio.to_thread(container.pause)
            container_info.status = "paused"
            logger.info(f"Paused Docker sandbox: {sandbox_id}")
        except Exception as e:
            logger.error(f"Failed to pause sandbox {sandbox_id}: {e}")
            raise

    async def resume(self, sandbox_id: str) -> None:
        """Resume paused Docker sandbox.

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        container_info = self._get_container_info(sandbox_id)
        container = container_info.container

        try:
            await asyncio.to_thread(container.unpause)
            container_info.status = "active"
            logger.info(f"Resumed Docker sandbox: {sandbox_id}")
        except Exception as e:
            logger.error(f"Failed to resume sandbox {sandbox_id}: {e}")
            raise

    async def destroy(self, sandbox_id: str) -> None:
        """Destroy Docker sandbox.

        Args:
            sandbox_id: Sandbox ID

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        container_info = self._containers.pop(sandbox_id, None)
        if not container_info:
            logger.warning(f"Sandbox {sandbox_id} not in cache, cannot destroy")
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        container = container_info.container

        try:
            # Stop and remove container
            await asyncio.to_thread(container.stop, timeout=5)
            await asyncio.to_thread(container.remove)
            logger.info(f"Destroyed Docker sandbox: {sandbox_id}")
        except Exception as e:
            logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")
            raise

    async def get_info(self, sandbox_id: str) -> SandboxInfo:
        """Get Docker sandbox information.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox information

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
        """
        container_info = self._get_container_info(sandbox_id)

        # Check actual container status from Docker API (not just cache)
        try:
            container = container_info.container
            container.reload()  # Refresh container state from Docker
            docker_status = container.status.lower()

            # Map Docker status to our status
            if docker_status == "running":
                actual_status = "active"
            elif docker_status in ("exited", "dead", "stopped"):
                actual_status = "stopped"
            elif docker_status == "paused":
                actual_status = "paused"
            else:
                actual_status = "stopped"  # Default to stopped for unknown states

            # Update cache if status changed
            if actual_status != container_info.status:
                logger.info(
                    f"Container {sandbox_id} status changed: {container_info.status} -> {actual_status}"
                )
                container_info.status = actual_status
        except Exception as e:
            logger.warning(f"Failed to get actual container status for {sandbox_id}: {e}")
            # If we can't check, assume stopped
            actual_status = "stopped"
            container_info.status = actual_status

        return SandboxInfo(
            sandbox_id=sandbox_id,
            status=container_info.status,
            created_at=container_info.created_at,
            provider="docker",
            template_id=container_info.template_id,
            metadata=container_info.metadata,
        )

    async def is_available(self) -> bool:
        """Check if Docker provider is available.

        Returns:
            True if Docker is installed and daemon is running
        """
        if not DOCKER_AVAILABLE:
            return False

        try:
            # Ping Docker daemon
            await asyncio.to_thread(self.docker_client.ping)
            return True
        except Exception as e:
            logger.warning(f"Docker not available: {e}")
            return False

    async def mount_nexus(
        self,
        sandbox_id: str,
        mount_path: str,
        nexus_url: str,
        api_key: str,
        agent_id: str | None = None,
        skip_dependency_checks: bool = False,  # noqa: ARG002 - Not used for Docker (always installs)
    ) -> dict[str, Any]:
        """Mount Nexus filesystem inside Docker sandbox via FUSE.

        Args:
            sandbox_id: Sandbox ID
            mount_path: Path where to mount (e.g., /mnt/nexus)
            nexus_url: Nexus server URL
            api_key: Nexus API key
            agent_id: Optional agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            skip_dependency_checks: Ignored for Docker (always checks/installs deps).
                Provided for interface compatibility with E2B provider.

        Returns:
            Mount status dict

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            RuntimeError: If mount fails
        """
        container_info = self._get_container_info(sandbox_id)
        container = container_info.container

        logger.info(f"[MOUNT-STEP-1] Starting mount process for sandbox {sandbox_id}")
        logger.info(f"[MOUNT-STEP-1] Mount path: {mount_path}, Nexus URL: {nexus_url}")

        # Transform localhost to host.docker.internal
        if "localhost" in nexus_url or "127.0.0.1" in nexus_url:
            original_url = nexus_url
            nexus_url = nexus_url.replace("localhost", "host.docker.internal")
            nexus_url = nexus_url.replace("127.0.0.1", "host.docker.internal")
            logger.info(f"[MOUNT-STEP-1] Transformed URL: {original_url} -> {nexus_url}")

        # Create mount directory
        logger.info(f"[MOUNT-STEP-2] Creating mount directory: {mount_path}")
        start_time = time.time()
        mkdir_result = await asyncio.to_thread(
            container.exec_run,
            f"mkdir -p {mount_path}",
        )
        logger.info(
            f"[MOUNT-STEP-2] mkdir completed in {time.time() - start_time:.2f}s, exit_code={mkdir_result.exit_code}"
        )
        if mkdir_result.exit_code != 0:
            error_msg = f"Failed to create mount directory: {mkdir_result.output.decode()}"
            logger.error(f"[MOUNT-STEP-2] {error_msg}")
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Check if nexus CLI is available
        logger.info("[MOUNT-STEP-3] Checking for nexus CLI...")
        start_time = time.time()
        check_result = await asyncio.to_thread(
            container.exec_run,
            "which nexus",
        )
        logger.info(
            f"[MOUNT-STEP-3] which nexus completed in {time.time() - start_time:.2f}s, exit_code={check_result.exit_code}"
        )

        if check_result.exit_code != 0:
            # nexus not found, try to install with FUSE support
            logger.info("[MOUNT-STEP-3] nexus CLI not found, installing nexus-ai-fs[fuse]...")
            start_time = time.time()
            install_result = await asyncio.to_thread(
                container.exec_run,
                "pip install -q 'nexus-ai-fs[fuse]'",
            )
            elapsed = time.time() - start_time
            logger.info(
                f"[MOUNT-STEP-3] pip install completed in {elapsed:.2f}s, exit_code={install_result.exit_code}"
            )

            if install_result.exit_code != 0:
                error_msg = f"Failed to install nexus-ai-fs: {install_result.output.decode()}"
                logger.error(f"[MOUNT-STEP-3] {error_msg}")
                return {
                    "success": False,
                    "mount_path": mount_path,
                    "message": error_msg,
                    "files_visible": 0,
                }
            logger.info("[MOUNT-STEP-3] Successfully installed nexus-ai-fs")
        else:
            logger.info("[MOUNT-STEP-3] nexus CLI already available, skipping installation")

        # Build mount command
        logger.info("[MOUNT-STEP-4] Building mount command...")
        logger.info(
            f"[MOUNT-STEP-4] nexus_url={nexus_url}, "
            f"api_key={'***' + api_key[-10:] if api_key else 'None'}"
            + (f", agent_id={agent_id}" if agent_id else "")
        )
        base_mount = (
            f"sudo NEXUS_API_KEY={api_key} "
            f"nexus mount {mount_path} "
            f"--remote-url {nexus_url} "
            f"--allow-other"
        )
        # Add agent-id for version attribution (issue #418)
        if agent_id:
            base_mount += f" --agent-id {agent_id}"
        mount_cmd = f"nohup {base_mount} > /tmp/nexus-mount.log 2>&1 &"
        logger.info(f"[MOUNT-STEP-4] Mount command: {mount_cmd}")

        # Run mount in background (wrap in shell)
        logger.info("[MOUNT-STEP-4] Executing mount command in background...")
        start_time = time.time()
        mount_result = await asyncio.to_thread(
            container.exec_run,
            ["sh", "-c", mount_cmd],
        )
        logger.info(
            f"[MOUNT-STEP-4] Mount command completed in {time.time() - start_time:.2f}s, exit_code={mount_result.exit_code}"
        )

        if mount_result.exit_code != 0:
            error_msg = f"Failed to start mount: {mount_result.output.decode()}"
            logger.error(f"[MOUNT-STEP-4] {error_msg}")
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Wait for mount to initialize
        logger.info("[MOUNT-STEP-5] Waiting for mount process to start (3 seconds)...")
        await asyncio.sleep(3)

        # Pre-warm the FUSE mount by triggering a simple operation from inside container
        # This establishes the connection and warms up caches BEFORE we do the verification ls
        logger.info("[MOUNT-STEP-5] Pre-warming FUSE mount with test access...")
        prewarm_start = time.time()
        prewarm_result = await asyncio.to_thread(
            container.exec_run,
            f"test -d {mount_path}",  # Simple directory test, doesn't list contents
        )
        prewarm_elapsed = time.time() - prewarm_start
        logger.info(
            f"[MOUNT-STEP-5] Pre-warm test took {prewarm_elapsed:.2f}s, exit_code={prewarm_result.exit_code}"
        )

        prewarm_success = prewarm_result.exit_code == 0
        if prewarm_success:
            logger.info("[MOUNT-STEP-5] Pre-warm successful, FUSE connection established")
        else:
            logger.warning(
                f"[MOUNT-STEP-5] Pre-warm failed but continuing: {prewarm_result.output.decode() if prewarm_result.output else 'no output'}"
            )

        # Verify mount by listing directory (use simple ls, not ls -la)
        # After pre-warming, this should be fast
        logger.info(f"[MOUNT-STEP-6] Verifying mount by listing {mount_path}...")
        start_time = time.time()
        try:
            ls_result = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run,
                    f"timeout 10 ls {mount_path}",  # 10 second timeout (should be fast after prewarm)
                ),
                timeout=15.0,  # 15 second timeout for the whole operation
            )
            elapsed = time.time() - start_time
            logger.info(
                f"[MOUNT-STEP-6] ls completed in {elapsed:.2f}s, exit_code={ls_result.exit_code}"
            )
            if elapsed > 5.0:
                logger.warning(
                    f"[MOUNT-STEP-6] ls took {elapsed:.2f}s - this is the known 'first ls slow' issue"
                )
                logger.info(
                    "[MOUNT-STEP-6] Subsequent ls commands will be fast once cache is populated"
                )
        except TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[MOUNT-STEP-6] ls command timed out after {elapsed:.2f}s")
            logger.error(
                "[MOUNT-STEP-6] This may indicate FUSE mount issues beyond normal first-access slowness"
            )
            # Continue anyway - mount might be working but slow
            ls_result = None

        # Check mount log for success message
        logger.info("[MOUNT-STEP-6] Checking mount log for success message...")
        log_check = await asyncio.to_thread(
            container.exec_run,
            "cat /tmp/nexus-mount.log 2>/dev/null || echo 'log not found'",  # Use cat instead of tail, handle missing file
        )
        mount_log_shows_success = False
        if log_check.exit_code == 0 and log_check.output:
            log_output = log_check.output.decode()
            if log_output and "log not found" not in log_output:
                logger.info(
                    f"[MOUNT-STEP-6] Mount log content (length={len(log_output)}):\n{log_output}"
                )
                if "Mounted Nexus to" in log_output:
                    mount_log_shows_success = True
                    logger.info(
                        "[MOUNT-STEP-6] Found 'Mounted Nexus to' in mount log - mount is successful"
                    )
            else:
                logger.warning("[MOUNT-STEP-6] Mount log not found or empty")
        else:
            logger.warning(
                f"[MOUNT-STEP-6] Mount log check command failed: exit_code={log_check.exit_code}"
            )

        # Handle successful ls
        if ls_result and ls_result.exit_code == 0:
            output = ls_result.output.decode() if ls_result.output else ""
            logger.info(f"[MOUNT-STEP-6] ls output: '{output}' (length={len(output)})")

            if output:
                # Count files (one per line in simple ls output)
                lines = [line for line in output.strip().split("\n") if line.strip()]
                file_count = len(lines)
                logger.info(
                    f"[MOUNT-STEP-6] Successfully mounted Nexus at {mount_path} "
                    f"with {file_count} items visible: {lines}"
                )
                return {
                    "success": True,
                    "mount_path": mount_path,
                    "message": f"Nexus mounted successfully at {mount_path}",
                    "files_visible": file_count,
                }
            elif mount_log_shows_success or prewarm_success:
                # ls returned empty but either mount log shows success OR pre-warm was successful
                # This can happen with timeout command or when mount log hasn't flushed yet
                reason = (
                    "mount log confirms success"
                    if mount_log_shows_success
                    else "pre-warm test succeeded"
                )
                logger.info(f"[MOUNT-STEP-6] ls returned empty output but {reason}")
                return {
                    "success": True,
                    "mount_path": mount_path,
                    "message": f"Nexus mounted successfully at {mount_path}",
                    "files_visible": -1,  # Unknown count but mount is working
                }
            else:
                logger.warning(
                    "[MOUNT-STEP-6] ls succeeded but returned empty output, no mount log success, and pre-warm failed"
                )

        # Handle timeout or error - still consider mount successful if log shows success
        if ls_result is None:
            logger.warning(
                "[MOUNT-STEP-6] Mount verification timed out, but mount process may be working"
            )

            # Check mount log for success message
            if log_check.exit_code == 0 and log_check.output:
                log_text = log_check.output.decode()
                if "Mounted Nexus to" in log_text:
                    logger.info(
                        "[MOUNT-STEP-6] Mount log shows successful mount, considering mount successful despite slow ls"
                    )
                    return {
                        "success": True,
                        "mount_path": mount_path,
                        "message": f"Nexus mounted at {mount_path} (note: ls is slow due to issue #391)",
                        "files_visible": -1,  # -1 indicates unknown due to timeout
                    }

            # Fallback: check if mount process is running
            logger.info("[MOUNT-STEP-6] Checking if mount process is running...")
            ps_result = await asyncio.to_thread(
                container.exec_run,
                "pgrep -f 'nexus mount' || ps aux | grep -v grep | grep nexus",
            )
            if ps_result.exit_code == 0 and ps_result.output:
                logger.info(
                    f"[MOUNT-STEP-6] Found mount-related process: {ps_result.output.decode()[:200]}"
                )
                return {
                    "success": True,
                    "mount_path": mount_path,
                    "message": f"Nexus mounted at {mount_path} (note: ls is slow due to issue #391)",
                    "files_visible": -1,  # -1 indicates unknown due to timeout
                }

        # Mount verification failed
        error_msg = f"Mount verification failed: {ls_result.output.decode() if ls_result and ls_result.output else 'No output or timeout'}"
        logger.error(f"[MOUNT-STEP-6] {error_msg}")
        if ls_result:
            logger.error(f"[MOUNT-STEP-6] Exit code: {ls_result.exit_code}")

        return {
            "success": False,
            "mount_path": mount_path,
            "message": error_msg,
            "files_visible": 0,
        }

    # Internal methods

    def _get_container_info(self, sandbox_id: str) -> ContainerInfo:
        """Get container info from cache.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Container info

        Raises:
            SandboxNotFoundError: If sandbox not in cache
        """
        if sandbox_id not in self._containers:
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")
        return self._containers[sandbox_id]

    def _create_container(self, image: str, name: str | None = None) -> Any:  # -> Container
        """Create a Docker container.

        Args:
            image: Docker image to use
            name: Optional container name (will be sanitized for Docker)

        Returns:
            Container instance
        """
        # Sanitize name for Docker (alphanumeric, hyphens, underscores only)
        container_name = None
        if name:
            # Replace invalid chars with hyphens, remove leading/trailing hyphens
            sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
            sanitized = sanitized.strip("-_")
            if sanitized:
                container_name = sanitized

        return self.docker_client.containers.run(
            image=image,
            # Use image default CMD (sleep infinity in sandbox image)
            detach=True,
            name=container_name,  # Set container name if provided
            cap_add=["SYS_ADMIN"],  # Needed for FUSE
            devices=["/dev/fuse:/dev/fuse:rwm"],  # FUSE device access
            security_opt=[
                "no-new-privileges:false",  # Allow sudo for FUSE mounting
                "apparmor=unconfined",  # Disable AppArmor for FUSE
            ],
            mem_limit=self.memory_limit,
            cpu_quota=int(self.cpu_limit * 100000),
            cpu_period=100000,
            network_mode=self.network_name
            if self.network_name
            else None,  # Only set if network_name is provided
            remove=False,  # Don't auto-remove, we'll handle cleanup
        )

    def _ensure_image(self, image_name: str) -> None:
        """Ensure Docker image exists locally.

        Args:
            image_name: Image to check/pull
        """
        try:
            self.docker_client.images.get(image_name)
            logger.debug(f"Image {image_name} already exists")
        except NotFound as e:
            # auto_pull is disabled in local workflow; instruct caller to build the image
            raise RuntimeError(
                f"Image {image_name} not found. Build it with:\n"
                f"  docker build -t {image_name} -f Dockerfile .\n"
                f"or run docker/build.sh in the repo."
            ) from e

    def _build_command(self, language: str, code: str) -> list[str]:
        """Build execution command for language and code.

        Args:
            language: Programming language
            code: Code to execute

        Returns:
            Command as list of strings
        """
        runtime = self.SUPPORTED_LANGUAGES[language]

        if runtime == "python":
            return ["python", "-c", code]
        elif runtime == "node":
            return ["node", "-e", code]
        elif runtime == "bash":
            return ["bash", "-c", code]
        else:
            raise UnsupportedLanguageError(f"Unknown runtime: {runtime}")

    async def _cleanup_loop(self) -> None:
        """Background task to cleanup expired containers."""
        logger.info("Starting Docker sandbox cleanup loop")

        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)

                now = datetime.now(UTC)
                expired_ids = []

                # Find expired containers
                for sandbox_id, info in self._containers.items():
                    if now >= info.expires_at:
                        expired_ids.append(sandbox_id)

                # Destroy expired containers
                for sandbox_id in expired_ids:
                    try:
                        logger.info(f"Cleaning up expired sandbox: {sandbox_id}")
                        await self.destroy(sandbox_id)
                    except Exception as e:
                        logger.error(f"Failed to cleanup sandbox {sandbox_id}: {e}")

            except asyncio.CancelledError:
                logger.info("Cleanup loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")

    async def close(self) -> None:
        """Close provider and cleanup all resources."""
        logger.info("Closing Docker sandbox provider")

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        # Destroy all containers
        sandbox_ids = list(self._containers.keys())
        for sandbox_id in sandbox_ids:
            try:
                await self.destroy(sandbox_id)
            except Exception as e:
                logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")

        logger.info("Docker sandbox provider closed")
