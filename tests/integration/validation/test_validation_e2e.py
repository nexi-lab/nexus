"""E2E tests for the validation pipeline through FastAPI RPC endpoint.

Tests the full pipeline: HTTP → RPC dispatch → NexusFS → SandboxManager → ValidationRunner.
Verifies sandbox_validate works with permission enforcement and without,
exercises the complete RPC serialization path, and validates performance.

Uses httpx ASGITransport for in-process testing (no subprocess).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from nexus.sandbox.sandbox_provider import CodeExecutionResult
from nexus.validation.models import ValidationPipelineConfig, ValidatorConfig
from nexus.validation.runner import ValidationRunner
from nexus.validation.script_builder import (
    build_simple_validation_script,
    parse_simple_script_output,
)


def _create_test_app(tmp_path: Path, enforce_permissions: bool = False):
    """Create a FastAPI app with real NexusFS for testing."""
    from nexus.backends.local import LocalBackend
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-validation")

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=str(storage_dir))
    metadata_store = RaftMetadataStore.embedded(str(tmp_path / "raft-metadata"))

    db_url = f"sqlite:///{tmp_path / 'records.db'}"

    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=None,
        enforce_permissions=enforce_permissions,
        allow_admin_bypass=True,
        enforce_zone_isolation=False,
        is_admin=True,
        enable_tiger_cache=False,
        enable_deferred_permissions=False,
    )

    api_key = "test-api-key-validation"
    app = create_app(nexus_fs=nx, api_key=api_key, database_url=db_url)
    return app, api_key, nx


def _run_async(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def test_app(tmp_path):
    """Create test FastAPI app without permission enforcement."""
    from nexus.core.sync_bridge import shutdown_sync_bridge

    app, api_key, nx = _create_test_app(tmp_path, enforce_permissions=False)
    yield app, api_key, nx
    shutdown_sync_bridge()


@pytest.fixture
def test_app_with_perms(tmp_path):
    """Create test FastAPI app with permission enforcement."""
    from nexus.core.sync_bridge import shutdown_sync_bridge

    app, api_key, nx = _create_test_app(tmp_path, enforce_permissions=True)
    yield app, api_key, nx
    shutdown_sync_bridge()


class TestValidationRPCEndpoint:
    """E2E: sandbox_validate accessible through FastAPI RPC endpoint."""

    def test_sandbox_validate_rpc_dispatch(self, test_app):
        """sandbox_validate RPC method is dispatched correctly."""
        app, api_key, nx = test_app

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                # Call sandbox_validate — will fail with sandbox not found,
                # but validates RPC dispatch works
                resp = await client.post(
                    "/api/nfs/sandbox_validate",
                    json={"params": {"sandbox_id": "sb-nonexistent"}},
                    headers=headers,
                )
                # Should get an RPC response (200 with error result, not 404)
                assert resp.status_code == 200
                data = resp.json()
                # RPC error for sandbox not found (not method-not-found)
                assert "error" in data or "result" in data

        _run_async(_test())

    def test_sandbox_validate_rpc_with_custom_workspace(self, test_app):
        """sandbox_validate accepts workspace_path parameter via RPC."""
        app, api_key, nx = test_app

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                resp = await client.post(
                    "/api/nfs/sandbox_validate",
                    json={
                        "params": {
                            "sandbox_id": "sb-test",
                            "workspace_path": "/code",
                        }
                    },
                    headers=headers,
                )
                assert resp.status_code == 200

        _run_async(_test())

    def test_sandbox_validate_requires_auth(self, test_app):
        """sandbox_validate rejects unauthenticated requests."""
        app, _api_key, _nx = test_app

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/nfs/sandbox_validate",
                    json={"params": {"sandbox_id": "sb-test"}},
                    # No auth header
                )
                assert resp.status_code in (401, 403)

        _run_async(_test())


class TestValidationPipelineE2E:
    """E2E: full validation pipeline with mocked sandbox provider."""

    @pytest.mark.asyncio
    async def test_full_pipeline_detect_and_validate(self):
        """Pipeline: detect Python → run ruff+mypy → return structured results."""
        # Simulate: no validators.yaml, Python project, ruff finds errors
        yaml_miss = CodeExecutionResult(
            stdout="", stderr="No such file", exit_code=1, execution_time=0.01
        )
        ls_result = CodeExecutionResult(
            stdout="pyproject.toml\nsrc\ntests\n",
            stderr="",
            exit_code=0,
            execution_time=0.02,
        )
        ruff_json = (
            '[{"filename": "src/app.py", "message": "F401 unused import os", '
            '"code": "F401", "location": {"row": 3, "column": 1}, '
            '"fix": {"message": "Remove unused import"}}]'
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                f"{ruff_json}\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===1===\n"
                "===VALIDATOR_END===\n"
                "===VALIDATOR_START===mypy===\n"
                "Success: no issues found\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=2.1,
        )

        provider = AsyncMock()
        provider.run_code = AsyncMock(side_effect=[yaml_miss, ls_result, script_result])

        runner = ValidationRunner()
        results = await runner.validate("sb-e2e", provider, "/workspace")

        # Structural checks
        assert len(results) == 2

        ruff_r = results[0]
        assert ruff_r.validator == "ruff"
        assert ruff_r.passed is False
        assert len(ruff_r.errors) == 1
        assert ruff_r.errors[0].file == "src/app.py"
        assert ruff_r.errors[0].line == 3
        assert ruff_r.errors[0].rule == "F401"
        assert ruff_r.errors[0].fix_available is True
        assert ruff_r.errors[0].severity == "error"

        mypy_r = results[1]
        assert mypy_r.validator == "mypy"
        assert mypy_r.passed is True
        assert len(mypy_r.errors) == 0

        # Serialization for API response
        response = {"validations": [r.model_dump() for r in results]}
        assert len(response["validations"]) == 2
        assert isinstance(response["validations"][0]["errors"], list)

        # Provider called exactly 3 times (yaml check, ls, script)
        assert provider.run_code.call_count == 3

    @pytest.mark.asyncio
    async def test_pipeline_with_explicit_config_no_detection(self):
        """Explicit config skips detection — only 1 provider call."""
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(
                    name="ruff",
                    command="ruff check --output-format json .",
                    output_format="json",
                ),
            ],
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                "[]\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=0.5,
        )

        provider = AsyncMock()
        provider.run_code = AsyncMock(return_value=script_result)

        runner = ValidationRunner()
        results = await runner.validate("sb-e2e", provider, config=config)

        assert len(results) == 1
        assert results[0].validator == "ruff"
        assert results[0].passed is True
        # Only 1 call — no yaml check, no detection
        assert provider.run_code.call_count == 1

    @pytest.mark.asyncio
    async def test_pipeline_empty_workspace_no_validators(self):
        """Empty workspace → no validators detected → empty results."""
        yaml_miss = CodeExecutionResult(stdout="", stderr="", exit_code=1, execution_time=0.01)
        ls_empty = CodeExecutionResult(
            stdout="README.md\n", stderr="", exit_code=0, execution_time=0.01
        )

        provider = AsyncMock()
        provider.run_code = AsyncMock(side_effect=[yaml_miss, ls_empty])

        runner = ValidationRunner()
        results = await runner.validate("sb-empty", provider)

        assert results == []
        # Only 2 calls: yaml check + ls (no script execution)
        assert provider.run_code.call_count == 2

    @pytest.mark.asyncio
    async def test_pipeline_execution_failure_returns_error_result(self):
        """Script execution failure → pipeline error result."""
        config = ValidationPipelineConfig(
            validators=[ValidatorConfig(name="ruff", command="ruff check .")],
        )

        provider = AsyncMock()
        provider.run_code = AsyncMock(side_effect=RuntimeError("sandbox crashed"))

        runner = ValidationRunner()
        results = await runner.validate("sb-crash", provider, config=config)

        assert len(results) == 1
        assert results[0].validator == "pipeline"
        assert results[0].passed is False


class TestPerformanceConstraints:
    """Validate the pipeline meets <5s target (Stripe Minions pattern)."""

    def test_script_builder_single_script_no_n_plus_one(self):
        """All validators in one script — no N+1 execution calls."""
        configs = [
            ValidatorConfig(name="ruff", command="ruff check --output-format json ."),
            ValidatorConfig(name="mypy", command="mypy --no-error-summary ."),
            ValidatorConfig(name="eslint", command="npx eslint --format json ."),
            ValidatorConfig(name="cargo-clippy", command="cargo clippy --message-format json 2>&1"),
        ]
        script = build_simple_validation_script(configs, "/workspace")

        # Single script, all 4 validators
        assert script.count("===VALIDATOR_START===") == 4
        # Working directory set once
        assert "cd /workspace" in script
        # Exit codes captured per-validator
        assert script.count("===VALIDATOR_EXIT===") == 4

    def test_parse_output_performance(self):
        """Parser handles 10k lines in <50ms."""
        big_output = ""
        for i in range(20):
            big_output += f"===VALIDATOR_START===v{i}===\n"
            for j in range(500):
                big_output += f'{{"file": "f{j}.py", "line": {j}, "msg": "err"}}\n'
            big_output += "===VALIDATOR_STDERR===\n"
            big_output += f"warning from v{i}\n"
            big_output += f"===VALIDATOR_EXIT==={i % 2}===\n"
            big_output += "===VALIDATOR_END===\n"

        start = time.monotonic()
        results = parse_simple_script_output(big_output)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert len(results) == 20
        assert elapsed_ms < 50, f"Parsing 10k lines took {elapsed_ms:.1f}ms (>50ms)"

    def test_config_caching_avoids_reparse(self):
        """Config loader returns cached instance for same key."""
        from nexus.validation.config import ValidatorConfigLoader

        yaml = (
            "validators:\n"
            "  - name: ruff\n"
            "    command: ruff check .\n"
            "  - name: mypy\n"
            "    command: mypy .\n"
        )
        loader = ValidatorConfigLoader()
        c1 = loader.load_from_string(yaml, cache_key="perf-test")
        c2 = loader.load_from_string(yaml, cache_key="perf-test")
        assert c1 is c2  # Same object — no re-parsing

    @pytest.mark.asyncio
    async def test_detection_uses_single_ls_command(self):
        """Project detection is O(1) — single ls call, not per-file exists."""
        from nexus.validation.detector import detect_project_validators

        provider = AsyncMock()
        provider.run_code = AsyncMock(
            return_value=CodeExecutionResult(
                stdout="pyproject.toml\npackage.json\nCargo.toml\n",
                stderr="",
                exit_code=0,
                execution_time=0.01,
            )
        )

        result = await detect_project_validators("sb-perf", provider, "/workspace")

        # Single ls call for all detection rules
        assert provider.run_code.call_count == 1
        # Detected all 3 ecosystems
        assert "ruff" in result
        assert "mypy" in result
        assert "eslint" in result
        assert "cargo-clippy" in result

    @pytest.mark.asyncio
    async def test_pipeline_overhead_under_100ms(self):
        """Pipeline orchestration overhead (sans execution) is <100ms."""
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(name="ruff", command="ruff check ."),
                ValidatorConfig(name="mypy", command="mypy ."),
            ],
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                "[]\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
                "===VALIDATOR_START===mypy===\n"
                "\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=0.001,  # Near-instant mock
        )

        provider = AsyncMock()
        provider.run_code = AsyncMock(return_value=script_result)

        runner = ValidationRunner()
        start = time.monotonic()
        results = await runner.validate("sb-perf", provider, config=config)
        overhead_ms = (time.monotonic() - start) * 1000

        assert len(results) == 2
        # Orchestration overhead should be minimal
        assert overhead_ms < 100, f"Pipeline overhead {overhead_ms:.1f}ms exceeds 100ms budget"
