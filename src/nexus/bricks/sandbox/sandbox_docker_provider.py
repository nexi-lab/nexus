"""Docker sandbox provider implementation.

Implements SandboxProvider interface using Docker containers for local code execution.
Designed for development and testing environments.
"""

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.bricks.sandbox.docker_mount_service import DockerMountService
from nexus.bricks.sandbox.sandbox_provider import (
    CodeExecutionResult,
    ExecutionTimeoutError,
    SandboxCreationError,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
    UnsupportedLanguageError,
    validate_language,
)

logger = logging.getLogger(__name__)

# Lazy import docker to avoid import errors if not installed
try:
    import docker
    import docker.errors
    from docker.errors import NotFound

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
        egress_proxy_enabled: bool = False,
        docker_host_alias: str | None = "host.docker.internal",
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
            egress_proxy_enabled: Enable shared egress proxy for network isolation.
                When True, profiles with allowed_egress_domains route through a
                Squid proxy on an internal Docker network instead of network=none.
            docker_host_alias: Hostname alias for localhost/127.0.0.1 inside Docker
                containers (default: "host.docker.internal"). Set to None to disable
                URL rewriting.
        """
        if not DOCKER_AVAILABLE:
            raise RuntimeError("docker package not installed. Install with: pip install docker")

        # Initialize Docker client with fallback for Colima
        if docker_client:
            self.docker_client = docker_client
        else:
            try:
                # Try default Docker socket
                self.docker_client = docker.from_env()
            except Exception as e:
                # Try Colima socket path
                import os

                colima_socket = os.path.expanduser("~/.colima/default/docker.sock")
                if os.path.exists(colima_socket):
                    self.docker_client = docker.DockerClient(base_url=f"unix://{colima_socket}")
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

        # Dev mode: Install nexus from local source instead of PyPI
        # Set NEXUS_DEV_MODE=1 to enable
        self._dev_mode = os.environ.get("NEXUS_DEV_MODE", "").lower() in ("1", "true", "yes")
        if self._dev_mode:
            # Get the nexus repo root (this file is at src/nexus/core/sandbox_docker_provider.py)
            self._nexus_src_path = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            )
            logger.info(
                "[DEV-MODE] Enabled - will install from local source: %s",
                self._nexus_src_path,
            )
        # Note: network_name is optional - if not set, containers will use default bridge network

        # Docker host alias for localhost URL rewriting inside containers
        self.docker_host_alias = docker_host_alias

        # Mount service for FUSE mount pipeline (Issue #2051)
        self._mount_service = DockerMountService(
            docker_host_alias=docker_host_alias,
            dev_mode=self._dev_mode,
            nexus_src_path=getattr(self, "_nexus_src_path", None),
        )

        # Egress proxy manager (lazily initialized when a profile needs egress)
        self._egress_proxy_enabled = egress_proxy_enabled
        self._egress_proxy: Any | None = None  # EgressProxyManager | None

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
        security_profile: Any | None = None,  # SandboxSecurityProfile | None
    ) -> str:
        """Create a new Docker sandbox.

        Args:
            template_id: Docker image to use (defaults to default_image)
            timeout_minutes: Sandbox TTL in minutes
            metadata: Additional metadata
            security_profile: Security profile for container settings.
                If None, defaults to SandboxSecurityProfile.standard().

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

            # Create container with security profile
            container = await asyncio.to_thread(
                self._create_container,
                image,
                container_name,
                security_profile,
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
                "Created Docker sandbox: %s (image=%s, ttl=%dm%s)",
                sandbox_id,
                image,
                timeout_minutes,
                name_info,
            )
            return sandbox_id

        except Exception as e:
            logger.error("Failed to create Docker sandbox: %s", e)
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
                logger.info("Resolved template '%s' to image: %s", template_id, template.image)
                image_name: str = template.image
                return image_name
            else:
                logger.warning(
                    "Template '%s' has no image configured, using as literal image name",
                    template_id,
                )
                return template_id

        # Treat as direct image name
        logger.debug("Using '%s' as direct image name", template_id)
        return template_id

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        as_script: bool = False,
    ) -> CodeExecutionResult:
        """Run code in Docker sandbox.

        Args:
            sandbox_id: Sandbox ID
            language: Programming language
            code: Code to execute
            timeout: Execution timeout in seconds
            as_script: If True, run as standalone script (stateless)

        Returns:
            Execution result

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            ExecutionTimeoutError: If execution times out
            UnsupportedLanguageError: If language not supported
        """
        # Docker always runs as script (stateless), so as_script is ignored
        _ = as_script

        # Validate language
        validate_language(language, self.SUPPORTED_LANGUAGES)

        # Get container
        container_info = self._get_container_info(sandbox_id)
        container = container_info.container

        # Build execution command
        cmd = self._build_command(language, code)

        # Execute code with timeout
        try:
            start_time = time.time()
            logger.info(
                "[DOCKER-EXEC] Starting execution in sandbox %s, timeout=%ds",
                sandbox_id,
                timeout,
            )
            logger.info("[DOCKER-EXEC] Command: %s", cmd)

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
            logger.info("[DOCKER-EXEC] Execution completed in %.2fs", execution_time)

            # Extract stdout and stderr (demux returns tuple)
            stdout_bytes, stderr_bytes = result.output
            stdout = stdout_bytes.decode("utf-8") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8") if stderr_bytes else ""

            logger.debug(
                "Executed %s code in sandbox %s: exit_code=%d, time=%.2fs",
                language,
                sandbox_id,
                result.exit_code,
                execution_time,
            )

            return CodeExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=result.exit_code,
                execution_time=execution_time,
            )

        except TimeoutError as timeout_err:
            logger.warning("Code execution timeout in sandbox %s", sandbox_id)
            raise ExecutionTimeoutError(
                f"Code execution exceeded {timeout} second timeout"
            ) from timeout_err
        except Exception as e:
            logger.error("Code execution failed in sandbox %s: %s", sandbox_id, e)
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
            logger.info("Paused Docker sandbox: %s", sandbox_id)
        except Exception as e:
            logger.error("Failed to pause sandbox %s: %s", sandbox_id, e)
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
            logger.info("Resumed Docker sandbox: %s", sandbox_id)
        except Exception as e:
            logger.error("Failed to resume sandbox %s: %s", sandbox_id, e)
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
            logger.warning("Sandbox %s not in cache, cannot destroy", sandbox_id)
            raise SandboxNotFoundError(f"Sandbox {sandbox_id} not found")

        container = container_info.container

        try:
            # Stop and remove container
            await asyncio.to_thread(container.stop, timeout=5)
            await asyncio.to_thread(container.remove)
            logger.info("Destroyed Docker sandbox: %s", sandbox_id)
        except Exception as e:
            logger.error("Failed to destroy sandbox %s: %s", sandbox_id, e)
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
                    "Container %s status changed: %s -> %s",
                    sandbox_id,
                    container_info.status,
                    actual_status,
                )
                container_info.status = actual_status
        except Exception as e:
            logger.warning("Failed to get actual container status for %s: %s", sandbox_id, e)
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
            logger.warning("Docker not available: %s", e)
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

        Delegates to DockerMountService for the full mount pipeline
        (Issue #2051: extracted from 373-line inline implementation).

        Args:
            sandbox_id: Sandbox ID
            mount_path: Path where to mount (e.g., /mnt/nexus)
            nexus_url: Nexus server URL
            api_key: Nexus API key
            agent_id: Optional agent ID for version attribution (issue #418).
            skip_dependency_checks: Ignored for Docker (always checks/installs deps).

        Returns:
            Mount status dict

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist
            RuntimeError: If mount fails
        """
        container_info = self._get_container_info(sandbox_id)
        return await self._mount_service.mount_nexus(
            container=container_info.container,
            mount_path=mount_path,
            nexus_url=nexus_url,
            api_key=api_key,
            agent_id=agent_id,
        )

    async def unmount_nexus(
        self,
        sandbox_id: str,
        mount_path: str = "/mnt/nexus",
    ) -> dict[str, Any]:
        """Unmount Nexus FUSE from a Docker sandbox.

        Args:
            sandbox_id: Sandbox ID.
            mount_path: Path where Nexus is mounted.

        Returns:
            Unmount status dict with success, mount_path, message.

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        container_info = self._get_container_info(sandbox_id)
        return await self._mount_service.unmount_nexus(
            container=container_info.container,
            mount_path=mount_path,
        )

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

    def _sanitize_container_name(self, name: str | None) -> str | None:
        """Sanitize a container name for Docker (alphanumeric, hyphens, underscores).

        Args:
            name: Raw container name (may contain invalid chars)

        Returns:
            Sanitized name, or None if name is empty/None.
        """
        if not name:
            return None
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
        sanitized = sanitized.strip("-_")
        return sanitized or None

    def _ensure_container_name_available(self, container_name: str) -> None:
        """Stop and remove any existing container with the given name.

        Args:
            container_name: Docker container name to free up.
        """
        try:
            existing_container = self.docker_client.containers.get(container_name)
            logger.info(
                "Found existing container with name '%s', stopping and removing...",
                container_name,
            )
            try:
                existing_container.stop(timeout=5)
            except Exception as stop_err:
                logger.debug("Error stopping container (may already be stopped): %s", stop_err)
            existing_container.remove(force=True)
            logger.info("Removed existing container '%s'", container_name)
        except NotFound:
            # Container doesn't exist, this is the normal case
            pass
        except Exception as e:
            logger.warning("Error checking/removing existing container '%s': %s", container_name, e)
            # Continue anyway - the run() call will handle the conflict

    def _create_container(
        self,
        image: str,
        name: str | None = None,
        security_profile: Any | None = None,  # SandboxSecurityProfile | None
    ) -> Any:  # -> Container
        """Create a Docker container with security profile.

        Args:
            image: Docker image to use
            name: Optional container name (will be sanitized for Docker)
            security_profile: Security profile for container settings.
                If None, uses SandboxSecurityProfile.standard() as default.

        Returns:
            Container instance
        """
        # Lazy import to avoid circular dependency at module level
        from nexus.bricks.sandbox.security_profile import SandboxSecurityProfile

        profile = security_profile or SandboxSecurityProfile.standard()

        # Sanitize and ensure name is available
        container_name = self._sanitize_container_name(name)
        if container_name:
            self._ensure_container_name_available(container_name)

        # Build volumes for container
        volumes = {}
        if self._dev_mode:
            # Mount local nexus source for development (read-only to protect host)
            volumes[self._nexus_src_path] = {"bind": "/nexus-src", "mode": "ro"}
            logger.info("[DEV-MODE] Mounting %s -> /nexus-src", self._nexus_src_path)

        # Get security settings from profile
        docker_kwargs = profile.to_docker_kwargs()

        # If egress proxy is enabled and profile has specific egress domains,
        # route through the shared proxy instead of using network_mode=none
        if (
            self._egress_proxy_enabled
            and profile.allowed_egress_domains
            and profile.allowed_egress_domains != ("*",)
        ):
            proxy_config = self._get_egress_proxy_config(profile.allowed_egress_domains)
            if proxy_config:
                # Override network_mode with egress network
                docker_kwargs.pop("network_mode", None)
                # Merge proxy environment with profile environment
                existing_env = docker_kwargs.get("environment", {})
                proxy_env = proxy_config.pop("environment", {})
                existing_env.update(proxy_env)
                docker_kwargs["environment"] = existing_env
                docker_kwargs.update(proxy_config)

        # Only mount /dev/fuse when FUSE is allowed by the profile
        devices = ["/dev/fuse:/dev/fuse:rwm"] if profile.allow_fuse else []

        return self.docker_client.containers.run(
            image=image,
            detach=True,
            name=container_name,
            devices=devices if devices else None,
            remove=False,  # Don't auto-remove, we'll handle cleanup
            volumes=volumes if volumes else None,
            **docker_kwargs,
        )

    def _get_egress_proxy_config(
        self,
        allowed_domains: tuple[str, ...],
    ) -> dict[str, Any]:
        """Get egress proxy configuration for a container.

        Lazily initializes the shared egress proxy manager and returns
        the Docker kwargs needed to route the container through it.

        Args:
            allowed_domains: Domains to allow through the proxy.

        Returns:
            Dict of Docker kwargs to merge, or empty dict if proxy
            is not available.
        """
        try:
            if self._egress_proxy is None:
                from nexus.bricks.sandbox.egress_proxy import EgressProxyManager

                self._egress_proxy = EgressProxyManager(self.docker_client)

            config: dict[str, Any] = self._egress_proxy.get_container_network_config(
                allowed_domains
            )
            return config
        except Exception as e:
            logger.warning(
                "Egress proxy unavailable, container will use profile network_mode: %s",
                e,
            )
            return {}

    def _ensure_image(self, image_name: str) -> None:
        """Ensure Docker image exists locally.

        Args:
            image_name: Image to check/pull
        """
        try:
            self.docker_client.images.get(image_name)
            logger.debug("Image %s already exists", image_name)
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
                        logger.info("Cleaning up expired sandbox: %s", sandbox_id)
                        await self.destroy(sandbox_id)
                    except Exception as e:
                        logger.error("Failed to cleanup sandbox %s: %s", sandbox_id, e)

            except asyncio.CancelledError:
                logger.info("Cleanup loop cancelled")
                break
            except Exception as e:
                logger.error("Error in cleanup loop: %s", e)

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
                logger.error("Failed to destroy sandbox %s: %s", sandbox_id, e)

        # Cleanup egress proxy
        if self._egress_proxy is not None:
            try:
                self._egress_proxy.cleanup()
            except Exception as e:
                logger.warning("Error cleaning up egress proxy: %s", e)

        logger.info("Docker sandbox provider closed")
