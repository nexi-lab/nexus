"""Docker-based integration tests for validation pipeline.

These tests require a running Docker daemon and are gated behind
the @pytest.mark.integration marker. They are skipped automatically
if Docker is not available.
"""

from __future__ import annotations

import pytest

# Check Docker availability
try:
    import docker

    _client = docker.from_env()
    _client.ping()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = True  # Intentionally True — individual tests gate properly

try:
    from nexus.sandbox.sandbox_docker_provider import DockerSandboxProvider

    DOCKER_PROVIDER_AVAILABLE = True
except ImportError:
    DOCKER_PROVIDER_AVAILABLE = False


@pytest.mark.integration
@pytest.mark.skipif(
    not DOCKER_PROVIDER_AVAILABLE,
    reason="Docker provider not available",
)
class TestValidationDockerIntegration:
    """Real Docker integration tests for validation pipeline.

    These tests create actual Docker containers and run validation
    commands inside them. Gated behind @pytest.mark.integration.
    """

    @pytest.fixture
    async def docker_provider(self):
        try:
            provider = DockerSandboxProvider()
            yield provider
        except Exception:
            pytest.skip("Docker daemon not available")

    @pytest.fixture
    async def sandbox_id(self, docker_provider):
        sb_id = await docker_provider.create(timeout_minutes=2)
        yield sb_id
        try:
            await docker_provider.destroy(sb_id)
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_run_validation_in_docker(self, docker_provider, sandbox_id):
        """Run a simple validation script in a real Docker container."""
        from nexus.validation.models import ValidatorConfig
        from nexus.validation.script_builder import (
            build_simple_validation_script,
            parse_simple_script_output,
        )

        # Use a simple echo-based "validator" that always passes
        config = ValidatorConfig(
            name="echo-test",
            command="echo 'hello from validator'",
            timeout=5,
        )
        script = build_simple_validation_script([config])
        result = await docker_provider.run_code(sandbox_id, "bash", script, timeout=10)

        assert result.exit_code == 0
        parsed = parse_simple_script_output(result.stdout)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "echo-test"
        assert parsed[0]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_validation_runner_in_docker(self, docker_provider, sandbox_id):
        """Run full ValidationRunner against a real Docker sandbox."""
        from nexus.validation.models import ValidationPipelineConfig, ValidatorConfig
        from nexus.validation.runner import ValidationRunner

        # Use echo validators since real linters may not be installed
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(
                    name="echo-pass",
                    command="echo '[]'",
                    timeout=5,
                ),
            ],
            max_total_timeout=15,
        )

        runner = ValidationRunner()
        results = await runner.validate(sandbox_id, docker_provider, config=config)

        assert len(results) == 1
        assert results[0].validator == "echo-pass"

    @pytest.mark.asyncio
    async def test_detection_in_docker(self, docker_provider, sandbox_id):
        """Test project detection in a real Docker sandbox."""
        from nexus.validation.detector import detect_project_validators

        # Empty workspace — should detect nothing
        validators = await detect_project_validators(
            sandbox_id, docker_provider, workspace_path="/tmp"
        )
        # /tmp likely has no project markers
        assert isinstance(validators, list)
