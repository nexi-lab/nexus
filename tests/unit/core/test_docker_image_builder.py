"""Unit tests for Docker image builder."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock docker package if not installed
if "docker" not in sys.modules:

    class BuildError(Exception):
        """Mock BuildError exception."""

        pass

    class ImageNotFound(Exception):
        """Mock ImageNotFound exception."""

        pass

    docker_errors_mock = MagicMock()
    docker_errors_mock.BuildError = BuildError
    docker_errors_mock.ImageNotFound = ImageNotFound

    sys.modules["docker"] = MagicMock()
    sys.modules["docker.errors"] = docker_errors_mock
    sys.modules["docker"].errors = docker_errors_mock

from nexus.core.docker_image_builder import DockerImageBuilder


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    client = MagicMock()
    client.images = MagicMock()
    return client


@pytest.fixture
def builder(mock_docker_client):
    """Create a DockerImageBuilder with mocked client."""
    return DockerImageBuilder(docker_client=mock_docker_client, use_cache=True)


@pytest.fixture
def sample_dockerfile_override():
    """Sample Dockerfile override content."""
    return """FROM nexus-runtime:latest
USER root
RUN apt-get update && apt-get install -y ffmpeg
RUN pip install torch tensorflow
USER nexus
WORKDIR /home/nexus/workspace
"""


class TestDockerImageBuilder:
    """Test suite for DockerImageBuilder."""

    def test_initialization(self, mock_docker_client):
        """Test builder initialization."""
        builder = DockerImageBuilder(docker_client=mock_docker_client, use_cache=False)

        assert builder.docker_client == mock_docker_client
        assert builder.use_cache is False

    def test_initialization_without_client(self):
        """Test builder creates its own client if none provided."""
        with patch("docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client

            builder = DockerImageBuilder()

            assert builder.docker_client == mock_client
            mock_from_env.assert_called_once()

    def test_image_exists_true(self, builder, mock_docker_client):
        """Test image_exists returns True when image is found."""
        mock_docker_client.images.get.return_value = MagicMock()

        result = builder.image_exists("nexus-runtime:latest")

        assert result is True
        mock_docker_client.images.get.assert_called_once_with("nexus-runtime:latest")

    def test_image_exists_false(self, builder, mock_docker_client, monkeypatch):
        """Test image_exists returns False when image not found."""

        # Create a proper exception class
        class ImageNotFound(Exception):
            pass

        # Mock docker.errors.ImageNotFound
        mock_errors = MagicMock()
        mock_errors.ImageNotFound = ImageNotFound
        monkeypatch.setattr("docker.errors", mock_errors)

        mock_docker_client.images.get.side_effect = ImageNotFound("not found")

        result = builder.image_exists("nonexistent:latest")

        assert result is False

    def test_image_exists_error(self, builder, mock_docker_client, monkeypatch):
        """Test image_exists returns False on other errors."""

        # Create a proper exception class for ImageNotFound so the except clause works
        class ImageNotFound(Exception):
            pass

        # Mock docker.errors.ImageNotFound
        mock_errors = MagicMock()
        mock_errors.ImageNotFound = ImageNotFound
        monkeypatch.setattr("docker.errors", mock_errors)

        mock_docker_client.images.get.side_effect = Exception("Docker error")

        result = builder.image_exists("error:latest")

        assert result is False

    @pytest.mark.asyncio
    async def test_build_from_dockerfile_path(self, builder, mock_docker_client, tmp_path):
        """Test building from Dockerfile path."""
        # Create a temporary Dockerfile
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM python:3.11\nRUN echo hello")

        # Mock the build process
        mock_image = MagicMock()
        mock_image.id = "sha256:abc123"
        mock_logs = [{"stream": "Step 1/2 : FROM python:3.11\n"}]

        with patch.object(builder, "_build_image_sync", return_value=(mock_image, mock_logs)):
            result = await builder.build_from_dockerfile(
                dockerfile_path=str(dockerfile),
                image_name="test-image:latest",
                context_path=tmp_path,
            )

        assert result["success"] is True
        assert result["image_id"] == "sha256:abc123"
        assert result["image_name"] == "test-image:latest"
        assert len(result["logs"]) == 1

    @pytest.mark.asyncio
    async def test_build_from_dockerfile_override(
        self, builder, sample_dockerfile_override, tmp_path
    ):
        """Test building from inline Dockerfile override."""
        # Mock the build process
        mock_image = MagicMock()
        mock_image.id = "sha256:xyz789"
        mock_logs = [{"stream": "Step 1/5 : FROM nexus-runtime:latest\n"}]

        with patch.object(builder, "_build_image_sync", return_value=(mock_image, mock_logs)):
            result = await builder.build_from_dockerfile(
                dockerfile_override=sample_dockerfile_override,
                image_name="nexus-runtime-ml:latest",
                context_path=tmp_path,
            )

        assert result["success"] is True
        assert result["image_id"] == "sha256:xyz789"
        assert result["image_name"] == "nexus-runtime-ml:latest"
        # Verify temp file was created and cleaned up (we can't directly check cleanup in this test)

    @pytest.mark.asyncio
    async def test_build_without_dockerfile_or_override(self, builder, tmp_path):
        """Test that build fails if neither dockerfile_path nor dockerfile_override is provided."""
        result = await builder.build_from_dockerfile(
            image_name="test:latest", context_path=tmp_path
        )

        assert result["success"] is False
        assert "Either dockerfile_path or dockerfile_override must be provided" in result["error"]

    @pytest.mark.asyncio
    async def test_build_with_both_dockerfile_and_override(
        self, builder, tmp_path, sample_dockerfile_override
    ):
        """Test that build fails if both dockerfile_path and dockerfile_override are provided."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base")

        result = await builder.build_from_dockerfile(
            dockerfile_path=str(dockerfile),
            dockerfile_override=sample_dockerfile_override,
            image_name="test:latest",
            context_path=tmp_path,
        )

        assert result["success"] is False
        assert "Cannot specify both dockerfile_path and dockerfile_override" in result["error"]

    @pytest.mark.asyncio
    async def test_build_dockerfile_not_found(self, builder, tmp_path):
        """Test build failure when Dockerfile doesn't exist."""
        nonexistent_path = tmp_path / "nonexistent" / "Dockerfile"

        result = await builder.build_from_dockerfile(
            dockerfile_path=str(nonexistent_path), image_name="test:latest", context_path=tmp_path
        )

        assert result["success"] is False
        assert "Dockerfile not found" in result["error"]

    @pytest.mark.asyncio
    async def test_build_context_not_found(self, builder, tmp_path):
        """Test build failure when context directory doesn't exist."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base")
        nonexistent_context = tmp_path / "nonexistent"

        result = await builder.build_from_dockerfile(
            dockerfile_path=str(dockerfile),
            image_name="test:latest",
            context_path=nonexistent_context,
        )

        assert result["success"] is False
        assert "Build context not found" in result["error"]

    @pytest.mark.asyncio
    async def test_build_with_build_args(self, builder, tmp_path):
        """Test building with build arguments."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("ARG VERSION\nFROM python:${VERSION}")

        mock_image = MagicMock()
        mock_image.id = "sha256:build123"

        with patch.object(
            builder, "_build_image_sync", return_value=(mock_image, [])
        ) as mock_build:
            result = await builder.build_from_dockerfile(
                dockerfile_path=str(dockerfile),
                image_name="test:latest",
                context_path=tmp_path,
                build_args={"VERSION": "3.11"},
            )

        assert result["success"] is True
        # Verify build args were passed
        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["buildargs"] == {"VERSION": "3.11"}

    @pytest.mark.asyncio
    async def test_build_error_handling(self, builder, tmp_path, monkeypatch):
        """Test proper error handling during build."""

        # Create proper exception classes
        class BuildError(Exception):
            def __init__(self, reason, build_log=None):
                super().__init__(reason)
                self.build_log = build_log or []

        # Mock docker.errors
        mock_errors = MagicMock()
        mock_errors.BuildError = BuildError
        monkeypatch.setattr("docker.errors", mock_errors)

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base")

        # Mock build error
        build_error = BuildError("Build failed", [])
        with patch.object(builder, "_build_image_sync", side_effect=build_error):
            result = await builder.build_from_dockerfile(
                dockerfile_path=str(dockerfile), image_name="test:latest", context_path=tmp_path
            )

        assert result["success"] is False
        assert "Build error" in result["error"]

    @pytest.mark.asyncio
    async def test_build_generic_exception(self, builder, tmp_path, monkeypatch):
        """Test handling of generic exceptions during build."""

        # Create proper exception classes
        class BuildError(Exception):
            pass

        class ImageNotFound(Exception):
            pass

        # Mock docker.errors to ensure proper isolation
        mock_errors = MagicMock()
        mock_errors.BuildError = BuildError
        mock_errors.ImageNotFound = ImageNotFound
        monkeypatch.setattr("docker.errors", mock_errors)

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base")

        with patch.object(builder, "_build_image_sync", side_effect=Exception("Unexpected error")):
            result = await builder.build_from_dockerfile(
                dockerfile_path=str(dockerfile), image_name="test:latest", context_path=tmp_path
            )

        assert result["success"] is False
        assert "Unexpected error" in result["error"]

    @pytest.mark.asyncio
    async def test_build_temp_file_cleanup(self, builder, sample_dockerfile_override, tmp_path):
        """Test that temporary Dockerfile is cleaned up after build."""
        mock_image = MagicMock()
        mock_image.id = "sha256:temp123"

        created_temp_files = []

        # Track temp files created
        original_named_temp = tempfile.NamedTemporaryFile

        def track_temp_file(*args, **kwargs):
            temp = original_named_temp(*args, **kwargs)
            created_temp_files.append(Path(temp.name))
            return temp

        with (
            patch("tempfile.NamedTemporaryFile", side_effect=track_temp_file),
            patch.object(builder, "_build_image_sync", return_value=(mock_image, [])),
        ):
            result = await builder.build_from_dockerfile(
                dockerfile_override=sample_dockerfile_override,
                image_name="test:latest",
                context_path=tmp_path,
            )

        assert result["success"] is True
        # Verify temp file was created
        assert len(created_temp_files) > 0
        # Verify it was cleaned up (file should not exist)
        for temp_file in created_temp_files:
            assert not temp_file.exists()

    @pytest.mark.asyncio
    async def test_build_with_cache_disabled(self, mock_docker_client, tmp_path):
        """Test building with cache disabled."""
        builder = DockerImageBuilder(docker_client=mock_docker_client, use_cache=False)

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base")

        mock_image = MagicMock()
        mock_image.id = "sha256:nocache123"

        with patch.object(
            builder, "_build_image_sync", return_value=(mock_image, [])
        ) as mock_build:
            result = await builder.build_from_dockerfile(
                dockerfile_path=str(dockerfile), image_name="test:latest", context_path=tmp_path
            )

        assert result["success"] is True
        # Verify nocache was set to True
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["nocache"] is True

    def test_format_log_line_with_stream(self, builder):
        """Test formatting log lines with stream field."""
        log = {"stream": "Step 1/2 : FROM python:3.11\n"}
        formatted = builder._format_log_line(log)
        assert formatted == "Step 1/2 : FROM python:3.11"

    def test_format_log_line_with_status(self, builder):
        """Test formatting log lines with status field."""
        log = {"status": "Downloading", "id": "abc123"}
        formatted = builder._format_log_line(log)
        # Status field alone is returned
        assert formatted == "Downloading"

    def test_format_log_line_with_error(self, builder):
        """Test formatting log lines with error field."""
        log = {"error": "Build failed"}
        formatted = builder._format_log_line(log)
        assert "ERROR: Build failed" in formatted

    def test_format_log_line_unknown_format(self, builder):
        """Test formatting log lines with unknown format."""
        log = {"unknown": "data"}
        formatted = builder._format_log_line(log)
        assert formatted == str(log)

    def test_format_log_line_with_string(self, builder):
        """Test formatting log lines when input is already a string."""
        log = "Already a string log message"
        formatted = builder._format_log_line(log)
        assert formatted == "Already a string log message"

    @pytest.mark.asyncio
    async def test_build_temp_file_cleanup_failure(
        self, builder, sample_dockerfile_override, tmp_path, monkeypatch
    ):
        """Test that build continues even if temp file cleanup fails."""
        mock_image = MagicMock()
        mock_image.id = "sha256:cleanup123"

        # Track if unlink was called
        unlink_called = False

        def mock_unlink(self, *args, **kwargs):
            nonlocal unlink_called
            unlink_called = True
            raise OSError("Permission denied")

        monkeypatch.setattr(Path, "unlink", mock_unlink)

        with patch.object(builder, "_build_image_sync", return_value=(mock_image, [])):
            result = await builder.build_from_dockerfile(
                dockerfile_override=sample_dockerfile_override,
                image_name="test:latest",
                context_path=tmp_path,
            )

        # Build should still succeed even if cleanup fails
        assert result["success"] is True
        assert unlink_called

    @pytest.mark.asyncio
    async def test_pull_image_already_exists(self, builder):
        """Test pull_image when image already exists locally."""
        with patch.object(builder, "image_exists", return_value=True):
            result = await builder.pull_image("nexus-runtime:latest")

        assert result is True

    @pytest.mark.asyncio
    async def test_pull_image_success(self, builder, mock_docker_client):
        """Test successful image pull."""
        with patch.object(builder, "image_exists", return_value=False):
            mock_docker_client.images.pull.return_value = MagicMock()
            result = await builder.pull_image("python:3.11")

        assert result is True
        mock_docker_client.images.pull.assert_called_once_with("python:3.11")

    @pytest.mark.asyncio
    async def test_pull_image_failure(self, builder, mock_docker_client):
        """Test image pull failure."""
        with patch.object(builder, "image_exists", return_value=False):
            mock_docker_client.images.pull.side_effect = Exception("Network error")
            result = await builder.pull_image("nonexistent:latest")

        assert result is False

    def test_initialization_without_docker_package(self, monkeypatch):
        """Test that initialization fails gracefully when docker package is not available."""
        # Mock DOCKER_AVAILABLE to False
        import nexus.core.docker_image_builder as builder_module

        monkeypatch.setattr(builder_module, "DOCKER_AVAILABLE", False)

        with pytest.raises(RuntimeError, match="docker package not installed"):
            DockerImageBuilder()
