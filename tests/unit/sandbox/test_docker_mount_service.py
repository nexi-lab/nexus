"""Unit tests for DockerMountService (Issue #2051).

Tests mount pipeline phases, verification strategies, and unmount.
Uses mocked Docker container exec_run to simulate different scenarios.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.bricks.sandbox.docker_mount_service import DockerMountService


def make_exec_result(exit_code: int = 0, output: str = "") -> MagicMock:
    """Create a mock Docker exec_run result."""
    result = MagicMock()
    result.exit_code = exit_code
    result.output = output.encode("utf-8") if output else b""
    return result


def make_container(
    exec_results: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock Docker container.

    Args:
        exec_results: List of exec_run results returned in order.
            If None, returns success for all calls.
    """
    container = MagicMock()
    if exec_results:
        container.exec_run.side_effect = exec_results
    else:
        container.exec_run.return_value = make_exec_result(0, "ok")
    return container


class TestValidateAndTransformUrl:
    """Tests for URL validation and localhost transformation."""

    def test_transforms_localhost_to_docker_host(self):
        svc = DockerMountService(docker_host_alias="host.docker.internal")

        result = svc._validate_and_transform_url("http://localhost:8000")
        assert "host.docker.internal" in result
        assert "localhost" not in result

    def test_transforms_127_0_0_1_to_docker_host(self):
        svc = DockerMountService(docker_host_alias="host.docker.internal")

        result = svc._validate_and_transform_url("http://127.0.0.1:8000")
        assert "host.docker.internal" in result
        assert "127.0.0.1" not in result

    def test_preserves_non_localhost_urls(self):
        svc = DockerMountService(docker_host_alias="host.docker.internal")

        result = svc._validate_and_transform_url("https://api.example.com:8000")
        assert result == "https://api.example.com:8000"

    def test_no_transform_when_alias_is_none(self):
        svc = DockerMountService(docker_host_alias=None)

        result = svc._validate_and_transform_url("http://localhost:8000")
        assert result == "http://localhost:8000"

    def test_validates_url_scheme(self):
        svc = DockerMountService()

        with pytest.raises(ValueError, match="must be http or https"):
            svc._validate_and_transform_url("ftp://example.com")

    def test_rejects_shell_metacharacters(self):
        svc = DockerMountService()

        with pytest.raises(ValueError, match="shell metacharacters"):
            svc._validate_and_transform_url("http://example.com;rm -rf /")


class TestVerifyMount:
    """Tests for mount verification strategies."""

    @pytest.mark.asyncio
    async def test_success_with_files_visible(self):
        """Strategy 1: ls succeeds with files listed."""
        svc = DockerMountService()
        container = MagicMock()

        # prewarm succeeds
        prewarm_result = make_exec_result(0, "")
        # ls succeeds with files
        ls_result = make_exec_result(0, "file1.txt\nfile2.txt\ndir1")
        # log check
        log_result = make_exec_result(0, "log not found")

        container.exec_run.side_effect = [prewarm_result, ls_result, log_result]

        result = await svc._verify_mount(container, "/mnt/nexus")

        assert result["success"] is True
        assert result["files_visible"] == 3

    @pytest.mark.asyncio
    async def test_success_with_empty_ls_but_mount_log(self):
        """Strategy 2: ls empty but mount log shows success."""
        svc = DockerMountService()
        container = MagicMock()

        # prewarm succeeds
        prewarm_result = make_exec_result(0, "")
        # ls succeeds but empty
        ls_result = make_exec_result(0, "")
        # log check shows success
        log_result = make_exec_result(0, "Mounted Nexus to /mnt/nexus")

        container.exec_run.side_effect = [prewarm_result, ls_result, log_result]

        result = await svc._verify_mount(container, "/mnt/nexus")

        assert result["success"] is True
        assert result["files_visible"] == -1  # Unknown count

    @pytest.mark.asyncio
    async def test_success_with_empty_ls_and_prewarm_success(self):
        """Strategy 2b: ls empty but prewarm succeeded."""
        svc = DockerMountService()
        container = MagicMock()

        # prewarm succeeds
        prewarm_result = make_exec_result(0, "")
        # ls succeeds but empty
        ls_result = make_exec_result(0, "")
        # No mount log
        log_result = make_exec_result(0, "log not found")

        container.exec_run.side_effect = [prewarm_result, ls_result, log_result]

        result = await svc._verify_mount(container, "/mnt/nexus")

        assert result["success"] is True
        assert result["files_visible"] == -1

    @pytest.mark.asyncio
    async def test_failure_when_all_strategies_fail(self):
        """All strategies fail -> mount verification fails."""
        svc = DockerMountService()
        container = MagicMock()

        # prewarm fails
        prewarm_result = make_exec_result(1, "error")
        # ls fails
        ls_result = make_exec_result(1, "No such file or directory")
        # No mount log
        log_result = make_exec_result(1, "")

        container.exec_run.side_effect = [prewarm_result, ls_result, log_result]

        result = await svc._verify_mount(container, "/mnt/nexus")

        assert result["success"] is False


class TestBuildMountCommand:
    """Tests for mount command construction."""

    def test_basic_mount_command(self):
        svc = DockerMountService()

        cmd = svc._build_mount_command(
            mount_path="/mnt/nexus",
            nexus_url="http://example.com:8000",
            api_key_source="NEXUS_API_KEY=test123 ",
            agent_id=None,
        )

        assert "/mnt/nexus" in cmd
        assert "--remote-url http://example.com:8000" in cmd
        assert "--daemon" in cmd
        assert "--allow-other" in cmd

    def test_mount_command_with_agent_id(self):
        svc = DockerMountService()

        cmd = svc._build_mount_command(
            mount_path="/mnt/nexus",
            nexus_url="http://example.com:8000",
            api_key_source="NEXUS_API_KEY=test123 ",
            agent_id="agent-001",
        )

        assert "--agent-id agent-001" in cmd

    def test_mount_command_without_agent_id(self):
        svc = DockerMountService()

        cmd = svc._build_mount_command(
            mount_path="/mnt/nexus",
            nexus_url="http://example.com:8000",
            api_key_source="NEXUS_API_KEY=test123 ",
            agent_id=None,
        )

        assert "--agent-id" not in cmd
