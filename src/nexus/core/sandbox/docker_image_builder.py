"""Docker image builder for custom sandbox images.

This module provides functionality to build Docker images from Dockerfiles.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import docker
try:
    import docker.errors

    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    logger.warning("docker package not installed. DockerImageBuilder will not work.")


class DockerImageBuilder:
    """Builds custom Docker images from Dockerfiles."""

    def __init__(
        self,
        docker_client: Any | None = None,
        use_cache: bool = True,
    ):
        """Initialize image builder.

        Args:
            docker_client: Docker client (defaults to docker.from_env())
            use_cache: Use Docker build cache for faster builds

        Raises:
            RuntimeError: If docker package is not installed
        """
        if not DOCKER_AVAILABLE:
            raise RuntimeError("docker package not installed. Install with: pip install docker")

        self.docker_client = docker_client or docker.from_env()  # type: ignore[attr-defined]
        self.use_cache = use_cache

    async def build_from_dockerfile(
        self,
        dockerfile_path: str | Path | None = None,
        dockerfile_override: str | None = None,
        image_name: str = "",
        context_path: str | Path = ".",
        build_args: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Build Docker image from Dockerfile.

        Args:
            dockerfile_path: Path to Dockerfile (mutually exclusive with dockerfile_override)
            dockerfile_override: Inline Dockerfile content to override base image (mutually exclusive with dockerfile_path)
            image_name: Full image name (e.g., nexus-runtime-ml:latest)
            context_path: Docker build context directory
            build_args: Optional build arguments

        Returns:
            Build result dict with keys:
                - success (bool): Whether build succeeded
                - image_id (str): Docker image ID if successful
                - image_name (str): Full image name
                - logs (list): Build logs
                - error (str): Error message if failed
        """
        # Validate input
        if not dockerfile_path and not dockerfile_override:
            error_msg = "Either dockerfile_path or dockerfile_override must be provided"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "logs": [],
            }

        if dockerfile_path and dockerfile_override:
            error_msg = "Cannot specify both dockerfile_path and dockerfile_override"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "logs": [],
            }

        context_path = Path(context_path)

        # Handle inline content by creating temporary Dockerfile
        if dockerfile_override:
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".Dockerfile", delete=False
            ) as temp_dockerfile:
                temp_dockerfile.write(dockerfile_override)
                temp_dockerfile.flush()
                dockerfile_path = Path(temp_dockerfile.name)
                logger.info(f"Building image {image_name} from inline Dockerfile override")
        else:
            assert dockerfile_path is not None  # Type guard
            dockerfile_path = Path(dockerfile_path)
            logger.info(f"Building image {image_name} from {dockerfile_path}")

        # Validate paths
        if not dockerfile_path.exists():
            error_msg = f"Dockerfile not found: {dockerfile_path}"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "logs": [],
            }

        if not context_path.exists():
            error_msg = f"Build context not found: {context_path}"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "logs": [],
            }

        # Build image
        try:
            # Run build in thread pool to avoid blocking
            image, logs = await asyncio.to_thread(
                self._build_image_sync,
                path=str(context_path),
                dockerfile=str(dockerfile_path),
                tag=image_name,
                buildargs=build_args,
                nocache=not self.use_cache,
                rm=True,  # Remove intermediate containers
            )

            logger.info(f"Successfully built image {image_name} (ID: {image.id[:12]})")

            return {
                "success": True,
                "image_id": image.id,
                "image_name": image_name,
                "logs": [self._format_log_line(line) for line in logs],
            }

        except docker.errors.BuildError as e:
            logger.error(f"Failed to build image {image_name}: {e}")
            return {
                "success": False,
                "error": f"Build error: {e}",
                "logs": [self._format_log_line(line) for line in e.build_log]
                if hasattr(e, "build_log")
                else [],
            }
        except Exception as e:
            logger.error(f"Failed to build image {image_name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "logs": [],
            }
        finally:
            # Clean up temporary Dockerfile if created
            if dockerfile_override and dockerfile_path and dockerfile_path.exists():
                try:
                    dockerfile_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary Dockerfile: {e}")

    def _build_image_sync(self, **kwargs: Any) -> tuple[Any, list[dict]]:
        """Synchronous image build (called in thread).

        Args:
            **kwargs: Arguments to pass to docker.images.build()

        Returns:
            Tuple of (image, logs)
        """
        image, logs = self.docker_client.images.build(**kwargs)
        return image, list(logs)

    def _format_log_line(self, log_entry: dict | str) -> str:
        """Format a log entry from Docker build.

        Args:
            log_entry: Log entry from Docker API

        Returns:
            Formatted log string
        """
        if isinstance(log_entry, str):
            return log_entry

        if isinstance(log_entry, dict):
            if "stream" in log_entry:
                return str(log_entry["stream"]).rstrip()
            elif "error" in log_entry:
                return f"ERROR: {log_entry['error']}"
            elif "status" in log_entry:
                return str(log_entry["status"])

        return str(log_entry)

    def image_exists(self, image_name: str) -> bool:
        """Check if image exists locally.

        Args:
            image_name: Image name to check

        Returns:
            True if image exists locally
        """
        try:
            self.docker_client.images.get(image_name)
            return True
        except docker.errors.ImageNotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking if image exists {image_name}: {e}")
            return False

    async def pull_image(self, image_name: str) -> bool:
        """Pull image if not available locally.

        Args:
            image_name: Image name to pull

        Returns:
            True if pull succeeded or image already exists
        """
        if self.image_exists(image_name):
            logger.info(f"Image already exists: {image_name}")
            return True

        logger.info(f"Pulling image: {image_name}")

        try:
            await asyncio.to_thread(self.docker_client.images.pull, image_name)
            logger.info(f"Successfully pulled {image_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to pull image {image_name}: {e}")
            return False
