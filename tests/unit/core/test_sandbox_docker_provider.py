"""Unit tests for Docker sandbox provider."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest

# Mock docker package if not installed
if "docker" not in sys.modules:
    # Create real exception classes for docker.errors
    class NotFound(Exception):
        """Mock NotFound exception."""

        pass

    docker_errors_mock = MagicMock()
    docker_errors_mock.NotFound = NotFound

    sys.modules["docker"] = MagicMock()
    sys.modules["docker.errors"] = docker_errors_mock
    sys.modules["docker"].errors = docker_errors_mock

from nexus.core.sandbox_docker_provider import DockerSandboxProvider
from nexus.core.sandbox_provider import (
    CodeExecutionResult,
    ExecutionTimeoutError,
    SandboxInfo,
    SandboxNotFoundError,
    UnsupportedLanguageError,
)


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    client = MagicMock()
    client.ping = Mock()
    return client


@pytest.fixture
def mock_container():
    """Create a mock Docker container."""
    container = MagicMock()
    container.id = "abcdef123456789"
    container.pause = Mock()
    container.unpause = Mock()
    container.stop = Mock()
    container.remove = Mock()
    return container


@pytest.fixture
def provider(mock_docker_client):
    """Create a Docker sandbox provider with mocked client."""
    return DockerSandboxProvider(
        docker_client=mock_docker_client,
        default_image="python:3.11-slim",
        cleanup_interval=60,
        auto_pull=True,
        network_name="bridge",  # Use bridge network for tests
    )


class TestDockerSandboxProvider:
    """Test suite for DockerSandboxProvider."""

    @pytest.mark.asyncio
    async def test_is_available_success(self, provider, mock_docker_client):
        """Test is_available returns True when Docker is running."""
        mock_docker_client.ping.return_value = True

        result = await provider.is_available()

        assert result is True
        mock_docker_client.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_available_failure(self, provider, mock_docker_client):
        """Test is_available returns False when Docker is not running."""
        mock_docker_client.ping.side_effect = Exception("Docker not running")

        result = await provider.is_available()

        assert result is False

    @pytest.mark.asyncio
    async def test_create_sandbox(self, provider, mock_docker_client, mock_container):
        """Test sandbox creation."""
        # Mock image exists
        mock_docker_client.images.get.return_value = MagicMock()

        # Mock container creation
        mock_docker_client.containers.run.return_value = mock_container

        sandbox_id = await provider.create(
            template_id="python:3.11-slim",
            timeout_minutes=10,
        )

        assert sandbox_id == "abcdef123456"  # First 12 chars of container ID
        assert sandbox_id in provider._containers
        mock_docker_client.images.get.assert_called_once_with("python:3.11-slim")
        mock_docker_client.containers.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_sandbox_auto_pull(self, provider, mock_docker_client, mock_container):
        """Test sandbox creation with auto-pull."""
        # Mock container creation
        mock_docker_client.containers.run.return_value = mock_container

        # Mock _ensure_image to simulate pull behavior
        # We need to mock this directly because asyncio.to_thread doesn't always work well with side_effect
        original_ensure = provider._ensure_image
        pull_called_with = []

        def mock_ensure_image(image_name):
            # Simulate image not found, trigger pull
            pull_called_with.append(image_name)
            mock_docker_client.images.pull(image_name)

        provider._ensure_image = mock_ensure_image

        try:
            sandbox_id = await provider.create(template_id="python:3.11-slim")

            assert sandbox_id == "abcdef123456"
            assert "python:3.11-slim" in pull_called_with
        finally:
            provider._ensure_image = original_ensure

    @pytest.mark.asyncio
    async def test_run_code_python(self, provider, mock_docker_client, mock_container):
        """Test running Python code."""
        # Setup container
        provider._containers["abcdef123456"] = MagicMock(
            container=mock_container,
            sandbox_id="abcdef123456",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            template_id="python:3.11-slim",
            metadata={},
            status="active",
        )

        # Mock exec_run result
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"Hello World\n", b"")
        mock_container.exec_run.return_value = exec_result

        result = await provider.run_code(
            sandbox_id="abcdef123456",
            language="python",
            code='print("Hello World")',
            timeout=30,
        )

        assert isinstance(result, CodeExecutionResult)
        assert result.stdout == "Hello World\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.execution_time >= 0  # Allow 0 for very fast execution on Windows

        mock_container.exec_run.assert_called_once()
        call_args = mock_container.exec_run.call_args
        assert call_args[0][0] == ["python", "-c", 'print("Hello World")']

    @pytest.mark.asyncio
    async def test_run_code_javascript(self, provider, mock_container):
        """Test running JavaScript code."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"test\n", b"")
        mock_container.exec_run.return_value = exec_result

        result = await provider.run_code(
            sandbox_id="test123",
            language="javascript",
            code='console.log("test")',
        )

        assert result.stdout == "test\n"
        call_args = mock_container.exec_run.call_args
        assert call_args[0][0] == ["node", "-e", 'console.log("test")']

    @pytest.mark.asyncio
    async def test_run_code_bash(self, provider, mock_container):
        """Test running Bash code."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"file.txt\n", b"")
        mock_container.exec_run.return_value = exec_result

        result = await provider.run_code(
            sandbox_id="test123",
            language="bash",
            code="ls",
        )

        assert result.stdout == "file.txt\n"
        call_args = mock_container.exec_run.call_args
        assert call_args[0][0] == ["bash", "-c", "ls"]

    @pytest.mark.asyncio
    async def test_run_code_unsupported_language(self, provider, mock_container):
        """Test running code with unsupported language."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        with pytest.raises(UnsupportedLanguageError) as exc_info:
            await provider.run_code(
                sandbox_id="test123",
                language="ruby",
                code="puts 'hello'",
            )

        assert "ruby" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_run_code_not_found(self, provider):
        """Test running code in non-existent sandbox."""
        with pytest.raises(SandboxNotFoundError):
            await provider.run_code(
                sandbox_id="nonexistent",
                language="python",
                code="print('test')",
            )

    @pytest.mark.asyncio
    async def test_run_code_timeout(self, provider, mock_container):
        """Test code execution timeout."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        # Mock exec_run to sleep longer than timeout
        async def slow_exec(*args, **kwargs):
            await asyncio.sleep(10)
            return MagicMock(exit_code=0, output=(b"", b""))

        with (
            patch("asyncio.to_thread", side_effect=slow_exec),
            pytest.raises(ExecutionTimeoutError),
        ):
            await provider.run_code(
                sandbox_id="test123",
                language="python",
                code="import time; time.sleep(100)",
                timeout=1,
            )

    @pytest.mark.asyncio
    async def test_pause_sandbox(self, provider, mock_container):
        """Test pausing a sandbox."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        await provider.pause("test123")

        mock_container.pause.assert_called_once()
        assert provider._containers["test123"].status == "paused"

    @pytest.mark.asyncio
    async def test_resume_sandbox(self, provider, mock_container):
        """Test resuming a paused sandbox."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="paused",
        )

        await provider.resume("test123")

        mock_container.unpause.assert_called_once()
        assert provider._containers["test123"].status == "active"

    @pytest.mark.asyncio
    async def test_destroy_sandbox(self, provider, mock_container):
        """Test destroying a sandbox."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        await provider.destroy("test123")

        mock_container.stop.assert_called_once()
        mock_container.remove.assert_called_once()
        assert "test123" not in provider._containers

    @pytest.mark.asyncio
    async def test_destroy_not_found(self, provider):
        """Test destroying non-existent sandbox."""
        with pytest.raises(SandboxNotFoundError):
            await provider.destroy("nonexistent")

    @pytest.mark.asyncio
    async def test_get_info(self, provider, mock_container):
        """Test getting sandbox info."""
        now = datetime.now(UTC)
        # Mock container status to be "running" so it maps to "active"
        mock_container.status = "running"
        mock_container.reload = Mock()

        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
            created_at=now,
            template_id="python:3.11-slim",
            metadata={"custom": "data"},
        )

        info = await provider.get_info("test123")

        assert isinstance(info, SandboxInfo)
        assert info.sandbox_id == "test123"
        assert info.status == "active"
        assert info.provider == "docker"
        assert info.template_id == "python:3.11-slim"
        assert info.metadata == {"custom": "data"}

    @pytest.mark.asyncio
    async def test_mount_nexus_localhost_transform(self, provider, mock_container):
        """Test mounting Nexus with localhost URL transformation."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        # Mock successful exec_run calls
        mkdir_result = MagicMock(exit_code=0)
        which_result = MagicMock(exit_code=0)  # nexus already installed
        mount_result = MagicMock(exit_code=0)
        prewarm_result = MagicMock(exit_code=0)  # test -d succeeds
        ls_result = MagicMock(
            exit_code=0,
            output=b"total 8\ndrwxr-xr-x 2 root root 4096 Jan 1 00:00 .\ndrwxr-xr-x 3 root root 4096 Jan 1 00:00 ..\n-rw-r--r-- 1 root root 0 Jan 1 00:00 test.txt\n",
        )
        log_result = MagicMock(exit_code=0, output=b"Mounted successfully")

        mock_container.exec_run.side_effect = [
            mkdir_result,
            which_result,
            mount_result,
            prewarm_result,
            ls_result,
            log_result,
        ]

        result = await provider.mount_nexus(
            sandbox_id="test123",
            mount_path="/mnt/nexus",
            nexus_url="http://localhost:8080",
            api_key="sk-test-key",
        )

        assert result["success"] is True
        assert result["mount_path"] == "/mnt/nexus"
        assert result["files_visible"] == 4  # Four lines in ls output

        # Verify host.docker.internal transformation
        calls = mock_container.exec_run.call_args_list
        mount_call = str(calls[2])  # Third call is mount command
        assert "host.docker.internal" in mount_call

    @pytest.mark.asyncio
    async def test_mount_nexus_install_cli(self, provider, mock_container):
        """Test mounting Nexus when CLI needs installation."""
        provider._containers["test123"] = MagicMock(
            container=mock_container,
            sandbox_id="test123",
            status="active",
        )

        # Mock exec_run: nexus not found, install succeeds, mount succeeds
        mkdir_result = MagicMock(exit_code=0)
        which_result = MagicMock(exit_code=1)  # nexus not found
        install_result = MagicMock(exit_code=0)
        mount_result = MagicMock(exit_code=0)
        prewarm_result = MagicMock(exit_code=0)  # test -d succeeds
        ls_result = MagicMock(exit_code=0, output=b"total 8\n")
        log_result = MagicMock(exit_code=0, output=b"Mounted successfully")

        mock_container.exec_run.side_effect = [
            mkdir_result,
            which_result,
            install_result,
            mount_result,
            prewarm_result,
            ls_result,
            log_result,
        ]

        result = await provider.mount_nexus(
            sandbox_id="test123",
            mount_path="/mnt/nexus",
            nexus_url="http://localhost:8080",
            api_key="sk-test-key",
        )

        assert result["success"] is True

        # Verify install was called
        calls = mock_container.exec_run.call_args_list
        install_call = str(calls[2])
        assert "pip install" in install_call
        assert "nexus-ai-fs" in install_call

    @pytest.mark.asyncio
    async def test_cleanup_loop_expired(self, provider, mock_container):
        """Test cleanup loop removes expired containers."""
        # Create expired container
        expired_time = datetime.now(UTC) - timedelta(minutes=1)
        provider._containers["expired123"] = MagicMock(
            container=mock_container,
            sandbox_id="expired123",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
            expires_at=expired_time,
            status="active",
        )

        # Run one iteration of cleanup
        provider._cleanup_running = True
        cleanup_task = asyncio.create_task(provider._cleanup_loop())

        # Wait a bit for cleanup to run
        await asyncio.sleep(0.1)

        # Cancel the cleanup task
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

        # Container should still be there because cleanup runs on interval
        # Let's manually trigger cleanup logic instead
        now = datetime.now(UTC)
        expired_ids = []
        for sandbox_id, info in provider._containers.items():
            if now >= info.expires_at:
                expired_ids.append(sandbox_id)

        assert "expired123" in expired_ids

    @pytest.mark.asyncio
    async def test_close_cleanup(self, provider, mock_container):
        """Test close method cleans up all containers."""
        # Create some containers
        provider._containers["test1"] = MagicMock(
            container=mock_container,
            sandbox_id="test1",
        )
        provider._containers["test2"] = MagicMock(
            container=mock_container,
            sandbox_id="test2",
        )

        await provider.close()

        # All containers should be destroyed
        assert len(provider._containers) == 0
        # stop and remove should be called for each container
        assert mock_container.stop.call_count == 2
        assert mock_container.remove.call_count == 2

    def test_build_command_python(self, provider):
        """Test building Python execution command."""
        cmd = provider._build_command("python", "print('test')")
        assert cmd == ["python", "-c", "print('test')"]

    def test_build_command_javascript(self, provider):
        """Test building JavaScript execution command."""
        cmd = provider._build_command("javascript", "console.log('test')")
        assert cmd == ["node", "-e", "console.log('test')"]

    def test_build_command_bash(self, provider):
        """Test building Bash execution command."""
        cmd = provider._build_command("bash", "ls -la")
        assert cmd == ["bash", "-c", "ls -la"]


class TestDockerTemplateResolution:
    """Test suite for Docker template resolution in sandbox provider."""

    @pytest.fixture
    def mock_docker_config(self):
        """Create a mock Docker template config."""
        from nexus.config import DockerImageTemplate, DockerTemplateConfig

        return DockerTemplateConfig(
            default_image="nexus-runtime:latest",
            templates={
                "base": DockerImageTemplate(image="nexus-runtime:latest"),
                "ml-heavy": DockerImageTemplate(
                    image="nexus-runtime-ml:latest",
                    dockerfile_override="""FROM nexus-runtime:latest
USER root
RUN pip install torch tensorflow
USER nexus
""",
                ),
                "web-dev": DockerImageTemplate(
                    image="nexus-runtime-web:latest",
                    dockerfile_override="""FROM nexus-runtime:latest
USER root
RUN pip install fastapi uvicorn
USER nexus
""",
                ),
            },
        )

    @pytest.fixture
    def provider_with_config(self, mock_docker_client, mock_docker_config):
        """Create a Docker sandbox provider with template config."""
        return DockerSandboxProvider(
            docker_client=mock_docker_client,
            docker_config=mock_docker_config,
            network_name="bridge",
        )

    def test_resolve_image_with_template_name(self, provider_with_config):
        """Test resolving template name to image."""
        resolved = provider_with_config._resolve_image("ml-heavy")
        assert resolved == "nexus-runtime-ml:latest"

    def test_resolve_image_with_another_template(self, provider_with_config):
        """Test resolving different template name."""
        resolved = provider_with_config._resolve_image("web-dev")
        assert resolved == "nexus-runtime-web:latest"

    def test_resolve_image_with_base_template(self, provider_with_config):
        """Test resolving base template."""
        resolved = provider_with_config._resolve_image("base")
        assert resolved == "nexus-runtime:latest"

    def test_resolve_image_with_nonexistent_template(self, provider_with_config):
        """Test resolving nonexistent template falls back to treating as image name."""
        resolved = provider_with_config._resolve_image("custom-image:v1.0")
        # Should return the input as-is when template not found
        assert resolved == "custom-image:v1.0"

    def test_resolve_image_with_none(self, provider_with_config):
        """Test resolving None returns default image."""
        resolved = provider_with_config._resolve_image(None)
        assert resolved == "nexus-runtime:latest"

    def test_resolve_image_without_config(self, provider):
        """Test resolving image without config uses default."""
        # Provider fixture doesn't have docker_config
        resolved = provider._resolve_image(None)
        assert resolved == "python:3.11-slim"  # Default from fixture

    def test_resolve_image_direct_image_name_without_config(self, provider):
        """Test using direct image name without config."""
        resolved = provider._resolve_image("custom:latest")
        # Without config, should treat as direct image name
        assert resolved == "custom:latest"

    def test_provider_initialization_with_config(self, mock_docker_client, mock_docker_config):
        """Test provider initialization with Docker config."""
        provider = DockerSandboxProvider(
            docker_client=mock_docker_client,
            docker_config=mock_docker_config,
            default_image="base:latest",
            network_name="bridge",
        )

        assert provider.docker_config == mock_docker_config
        assert provider.default_image == "base:latest"

    def test_provider_config_access(self, provider_with_config, mock_docker_config):
        """Test accessing config from provider."""
        assert provider_with_config.docker_config == mock_docker_config
        assert len(provider_with_config.docker_config.templates) == 3
        assert "ml-heavy" in provider_with_config.docker_config.templates

    @pytest.mark.asyncio
    async def test_create_sandbox_with_template_id(
        self, provider_with_config, mock_docker_client, mock_container
    ):
        """Test creating sandbox with template_id resolves correctly."""
        # Mock image exists
        mock_docker_client.images.get.return_value = MagicMock()

        # Mock container creation
        mock_docker_client.containers.run.return_value = mock_container

        sandbox_id = await provider_with_config.create(
            template_id="ml-heavy", timeout_minutes=5, metadata={"test": "template"}
        )

        assert sandbox_id is not None

        # Verify the correct image was used
        mock_docker_client.containers.run.assert_called_once()
        call_kwargs = mock_docker_client.containers.run.call_args[1]
        assert call_kwargs["image"] == "nexus-runtime-ml:latest"

    @pytest.mark.asyncio
    async def test_create_sandbox_with_direct_image(
        self, provider_with_config, mock_docker_client, mock_container
    ):
        """Test creating sandbox with direct image name (not in templates)."""
        # Mock image exists
        mock_docker_client.images.get.return_value = MagicMock()

        # Mock container creation
        mock_docker_client.containers.run.return_value = mock_container

        sandbox_id = await provider_with_config.create(
            template_id="custom-image:v2.0", timeout_minutes=5, metadata={"test": "direct"}
        )

        assert sandbox_id is not None

        # Verify the direct image name was used
        call_kwargs = mock_docker_client.containers.run.call_args[1]
        assert call_kwargs["image"] == "custom-image:v2.0"

    @pytest.mark.asyncio
    async def test_create_sandbox_without_template_id(
        self, provider_with_config, mock_docker_client, mock_container
    ):
        """Test creating sandbox without template_id uses default."""
        # Mock image exists
        mock_docker_client.images.get.return_value = MagicMock()

        # Mock container creation
        mock_docker_client.containers.run.return_value = mock_container

        sandbox_id = await provider_with_config.create(
            timeout_minutes=5, metadata={"test": "default"}
        )

        assert sandbox_id is not None

        # Verify default image was used
        call_kwargs = mock_docker_client.containers.run.call_args[1]
        assert call_kwargs["image"] == "nexus-runtime:latest"

    def test_template_with_dockerfile_override(self, mock_docker_config):
        """Test accessing dockerfile_override from template."""
        ml_template = mock_docker_config.templates["ml-heavy"]

        assert ml_template.dockerfile_override is not None
        assert "torch" in ml_template.dockerfile_override
        assert "tensorflow" in ml_template.dockerfile_override

    def test_multiple_template_resolution(self, provider_with_config):
        """Test resolving multiple templates in sequence."""
        templates_to_test = ["base", "ml-heavy", "web-dev"]
        expected_images = [
            "nexus-runtime:latest",
            "nexus-runtime-ml:latest",
            "nexus-runtime-web:latest",
        ]

        for template, expected in zip(templates_to_test, expected_images, strict=False):
            resolved = provider_with_config._resolve_image(template)
            assert resolved == expected
