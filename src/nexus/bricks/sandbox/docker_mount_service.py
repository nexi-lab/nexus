"""Docker mount service for FUSE mount pipeline (Issue #2051).

Extracts the mount_nexus pipeline from DockerSandboxProvider into a
focused service. Handles:
- URL validation and localhost transformation
- FUSE configuration inside containers
- Nexus CLI installation (dev/prod modes)
- Mount command building and execution
- Mount verification (multiple fallback strategies)
- Unmount support

Uses poll-based mount waiting instead of hardcoded sleep (Issue #2051 #14).
"""

import asyncio
import logging
import shlex
import time
from typing import Any

from nexus.bricks.sandbox.sandbox_provider import (
    validate_agent_id,
    validate_mount_path,
    validate_nexus_url,
)

logger = logging.getLogger(__name__)


class DockerMountService:
    """Service for mounting/unmounting Nexus FUSE inside Docker containers.

    Extracted from DockerSandboxProvider.mount_nexus to keep the
    provider focused on container lifecycle.

    Args:
        docker_host_alias: Hostname alias for localhost inside Docker
            (default: "host.docker.internal"). Set to None to disable
            URL rewriting.
        dev_mode: If True, install from PyPI then overlay local source.
        nexus_src_path: Local nexus source path (only used in dev_mode).
    """

    def __init__(
        self,
        docker_host_alias: str | None = "host.docker.internal",
        dev_mode: bool = False,
        nexus_src_path: str | None = None,
    ) -> None:
        self._docker_host_alias = docker_host_alias
        self._dev_mode = dev_mode
        self._nexus_src_path = nexus_src_path

    async def mount_nexus(
        self,
        container: Any,
        mount_path: str,
        nexus_url: str,
        api_key: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Full FUSE mount pipeline inside a Docker container.

        Phases:
            1. Validate inputs and transform localhost URLs
            2. Create mount directory and configure FUSE
            3. Ensure nexus CLI is available (install if needed)
            4. Build and execute mount command
            5. Verify mount succeeded

        Args:
            container: Docker container object.
            mount_path: Path where Nexus will be mounted.
            nexus_url: Nexus server URL.
            api_key: Nexus API key.
            agent_id: Optional agent ID for version attribution.

        Returns:
            Mount status dict with success, mount_path, message, files_visible.
        """
        # Phase 1: Validate and transform
        mount_path = validate_mount_path(mount_path)
        nexus_url = self._validate_and_transform_url(nexus_url)
        if agent_id is not None:
            agent_id = validate_agent_id(agent_id)

        logger.info(
            "[MOUNT] Starting mount for mount_path=%s, nexus_url=%s",
            mount_path,
            nexus_url,
        )

        # Phase 2: Prepare container (mkdir + FUSE config)
        prep_result = await self._prepare_container(container, mount_path)
        if not prep_result["success"]:
            return prep_result

        # Phase 3: Ensure nexus CLI
        cli_result = await self._ensure_nexus_cli(container)
        if not cli_result["success"]:
            return {
                "success": False,
                "mount_path": mount_path,
                "message": cli_result["message"],
                "files_visible": 0,
            }

        # Phase 4: Build and execute mount command
        api_key_source = await self._write_api_key(container, api_key)
        mount_cmd = self._build_mount_command(mount_path, nexus_url, api_key_source, agent_id)

        logger.info("[MOUNT] Executing mount command...")
        start_time = time.time()
        mount_result = await asyncio.to_thread(
            container.exec_run,
            ["sh", "-c", mount_cmd],
        )
        elapsed = time.time() - start_time
        logger.info(
            "[MOUNT] Mount command completed in %.2fs, exit_code=%d",
            elapsed,
            mount_result.exit_code,
        )

        if mount_result.exit_code != 0:
            error_msg = f"Failed to start mount: {mount_result.output.decode()}"
            logger.error("[MOUNT] %s", error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Phase 5: Poll-based wait + verify
        await self._poll_mount_ready(container, mount_path)
        return await self._verify_mount(container, mount_path)

    async def unmount_nexus(
        self,
        container: Any,
        mount_path: str = "/mnt/nexus",
    ) -> dict[str, Any]:
        """Unmount Nexus FUSE from a Docker container.

        Args:
            container: Docker container object.
            mount_path: Path where Nexus is mounted.

        Returns:
            Unmount status dict with success, mount_path, message.
        """
        mount_path = validate_mount_path(mount_path)

        logger.info("[UNMOUNT] Unmounting %s...", mount_path)

        # Try fusermount first (preferred)
        result = await asyncio.to_thread(
            container.exec_run,
            ["sh", "-c", f"fusermount -u {mount_path} 2>&1 || sudo umount {mount_path} 2>&1"],
        )

        if result.exit_code == 0:
            logger.info("[UNMOUNT] Successfully unmounted %s", mount_path)
            return {
                "success": True,
                "mount_path": mount_path,
                "message": f"Nexus unmounted from {mount_path}",
            }

        output = result.output.decode() if result.output else ""
        logger.warning("[UNMOUNT] Unmount failed: %s", output)
        return {
            "success": False,
            "mount_path": mount_path,
            "message": f"Unmount failed: {output}",
        }

    # -- Internal helpers ---------------------------------------------------

    def _validate_and_transform_url(self, nexus_url: str) -> str:
        """Validate URL and transform localhost for Docker networking.

        Args:
            nexus_url: Nexus server URL.

        Returns:
            Validated and potentially transformed URL.

        Raises:
            ValueError: If URL is invalid.
        """
        nexus_url = validate_nexus_url(nexus_url)

        if self._docker_host_alias and ("localhost" in nexus_url or "127.0.0.1" in nexus_url):
            original = nexus_url
            nexus_url = nexus_url.replace("localhost", self._docker_host_alias)
            nexus_url = nexus_url.replace("127.0.0.1", self._docker_host_alias)
            logger.info("[MOUNT] Transformed URL: %s -> %s", original, nexus_url)

        return nexus_url

    async def _prepare_container(self, container: Any, mount_path: str) -> dict[str, Any]:
        """Create mount directory and configure FUSE.

        Args:
            container: Docker container.
            mount_path: Mount path.

        Returns:
            Dict with success and optional error message.
        """
        # Create mount directory (list form avoids shell interpolation)
        mkdir_result = await asyncio.to_thread(
            container.exec_run,
            ["mkdir", "-p", mount_path],
        )
        if mkdir_result.exit_code != 0:
            error_msg = f"Failed to create mount directory: {mkdir_result.output.decode()}"
            logger.error("[MOUNT] %s", error_msg)
            return {
                "success": False,
                "mount_path": mount_path,
                "message": error_msg,
                "files_visible": 0,
            }

        # Configure FUSE for user_allow_other
        fuse_conf_cmd = (
            "grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || "
            "echo 'user_allow_other' | sudo tee -a /etc/fuse.conf > /dev/null"
        )
        fuse_result = await asyncio.to_thread(
            container.exec_run,
            ["sh", "-c", fuse_conf_cmd],
        )
        if fuse_result.exit_code != 0:
            logger.warning(
                "[MOUNT] Failed to configure FUSE (continuing anyway): %s",
                fuse_result.output.decode() if fuse_result.output else "",
            )

        return {"success": True}

    async def _ensure_nexus_cli(self, container: Any) -> dict[str, Any]:
        """Check if nexus CLI is available, install if needed.

        Args:
            container: Docker container.

        Returns:
            Dict with success and optional error message.
        """
        check_result = await asyncio.to_thread(
            container.exec_run,
            "which nexus",
        )

        needs_install = check_result.exit_code != 0 or self._dev_mode

        if not needs_install:
            logger.info("[MOUNT] nexus CLI already available")
            return {"success": True, "message": "already installed"}

        if self._dev_mode:
            logger.info("[MOUNT] Installing from PyPI + local source (dev mode)")
            install_cmd = (
                "pip install -q 'nexus-ai-fs[fuse]' && "
                "SITE_PACKAGES=$(python -c 'import site; print(site.getsitepackages()[0])') && "
                "cd /nexus-src/src && "
                "find nexus -name '*.py' -exec sudo cp --parents {} \"$SITE_PACKAGES/\" \\;"
            )
        else:
            logger.info("[MOUNT] Installing nexus-ai-fs[fuse]...")
            install_cmd = "pip install -q 'nexus-ai-fs[fuse]'"

        start_time = time.time()
        install_result = await asyncio.to_thread(
            container.exec_run,
            ["bash", "-c", install_cmd],
        )
        elapsed = time.time() - start_time

        if install_result.exit_code != 0:
            error_msg = f"Failed to install nexus-ai-fs: {install_result.output.decode()}"
            logger.error("[MOUNT] %s (%.2fs)", error_msg, elapsed)
            return {"success": False, "message": error_msg}

        logger.info("[MOUNT] Installed nexus-ai-fs in %.2fs", elapsed)
        return {"success": True, "message": "installed"}

    async def _write_api_key(self, container: Any, api_key: str) -> str:
        """Write API key to a temp file inside container (CWE-214 prevention).

        Args:
            container: Docker container.
            api_key: API key string.

        Returns:
            Shell snippet for sourcing the API key.
        """
        api_key_file = "/tmp/.nexus_api_key"
        quoted_key = shlex.quote(api_key)
        write_cmd = f"printf '%s' {quoted_key} > {api_key_file} && chmod 600 {api_key_file}"
        result = await asyncio.to_thread(
            container.exec_run,
            ["sh", "-c", write_cmd],
        )

        if result.exit_code != 0:
            logger.warning("[MOUNT] Failed to write API key file, falling back to env var")
            return f"NEXUS_API_KEY={shlex.quote(api_key)} "

        return f"NEXUS_API_KEY=$(cat {api_key_file}) "

    def _build_mount_command(
        self,
        mount_path: str,
        nexus_url: str,
        api_key_source: str,
        agent_id: str | None,
    ) -> str:
        """Build the nexus mount shell command.

        Args:
            mount_path: Mount path.
            nexus_url: Nexus server URL.
            api_key_source: Shell snippet for API key.
            agent_id: Optional agent ID.

        Returns:
            Shell command string.
        """
        cmd = (
            f"sudo {api_key_source}"
            f"nexus mount {mount_path} "
            f"--remote-url {nexus_url} "
            f"--allow-other "
            f"--daemon"
        )
        if agent_id:
            cmd += f" --agent-id {agent_id}"
        cmd += " 2>&1"
        return cmd

    async def _poll_mount_ready(
        self,
        container: Any,
        mount_path: str,
        max_attempts: int = 10,
        initial_delay: float = 0.2,
    ) -> bool:
        """Poll until mount directory is accessible.

        Uses exponential backoff: 0.2s, 0.4s, 0.8s, 1.6s, 1.6s, ...
        Max total wait: ~8 seconds (vs hardcoded 3s sleep previously).

        Args:
            container: Docker container.
            mount_path: Mount path to check.
            max_attempts: Maximum poll attempts.
            initial_delay: Initial delay in seconds.

        Returns:
            True if mount became ready, False if timed out.
        """
        for attempt in range(max_attempts):
            delay = initial_delay * (2 ** min(attempt, 3))
            await asyncio.sleep(delay)

            result = await asyncio.to_thread(
                container.exec_run,
                ["test", "-d", mount_path],
            )
            if result.exit_code == 0:
                logger.info("[MOUNT] Mount ready after %d polls", attempt + 1)
                return True

        logger.warning("[MOUNT] Mount not ready after %d polls", max_attempts)
        return False

    async def _verify_mount(
        self,
        container: Any,
        mount_path: str,
    ) -> dict[str, Any]:
        """Verify mount succeeded using multiple strategies.

        Strategies (in order):
            1. ls succeeds with files -> success
            2. ls empty + mount log shows success -> success
            3. ls empty + prewarm success -> success
            4. All fail -> failure

        Args:
            container: Docker container.
            mount_path: Mount path.

        Returns:
            Mount status dict.
        """
        # Pre-warm: simple directory test (list form avoids shell interpolation)
        prewarm_result = await asyncio.to_thread(
            container.exec_run,
            ["test", "-d", mount_path],
        )
        prewarm_success = prewarm_result.exit_code == 0

        # ls verification (with timeout, list form avoids shell interpolation)
        try:
            ls_result = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run,
                    ["timeout", "10", "ls", mount_path],
                ),
                timeout=15.0,
            )
        except TimeoutError:
            ls_result = None
            logger.warning("[MOUNT] ls timed out, checking mount log...")

        # Check mount log
        log_result = await asyncio.to_thread(
            container.exec_run,
            "cat /tmp/nexus-mount-*.log 2>/dev/null | tail -50 || echo 'log not found'",
        )
        mount_log_success = False
        if log_result.exit_code == 0 and log_result.output:
            log_text = log_result.output.decode()
            if "Mounted Nexus to" in log_text:
                mount_log_success = True

        # Strategy 1: ls succeeds with files
        if ls_result and ls_result.exit_code == 0:
            output = ls_result.output.decode() if ls_result.output else ""
            if output.strip():
                lines = [line for line in output.strip().split("\n") if line.strip()]
                return {
                    "success": True,
                    "mount_path": mount_path,
                    "message": f"Nexus mounted successfully at {mount_path}",
                    "files_visible": len(lines),
                }

            # Strategy 2: ls empty but mount log or prewarm success
            if mount_log_success or prewarm_success:
                reason = (
                    "mount log confirms success" if mount_log_success else "pre-warm test succeeded"
                )
                logger.info("[MOUNT] ls empty but %s", reason)
                return {
                    "success": True,
                    "mount_path": mount_path,
                    "message": f"Nexus mounted successfully at {mount_path}",
                    "files_visible": -1,
                }

        # Strategy 3: ls timed out but mount log shows success
        if ls_result is None and mount_log_success:
            return {
                "success": True,
                "mount_path": mount_path,
                "message": f"Nexus mounted at {mount_path} (ls slow)",
                "files_visible": -1,
            }

        # All strategies failed
        error_msg = (
            f"Mount verification failed: "
            f"{ls_result.output.decode() if ls_result and ls_result.output else 'timeout or no output'}"
        )
        logger.error("[MOUNT] %s", error_msg)
        return {
            "success": False,
            "mount_path": mount_path,
            "message": error_msg,
            "files_visible": 0,
        }
