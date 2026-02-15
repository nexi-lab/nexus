"""Tests for validation runner."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.sandbox.sandbox_provider import CodeExecutionResult
from nexus.validation.models import ValidationPipelineConfig, ValidatorConfig
from nexus.validation.runner import ValidationRunner


def _make_provider(responses: list[CodeExecutionResult]) -> AsyncMock:
    """Create mock provider that returns responses in sequence."""
    provider = AsyncMock()
    provider.run_code = AsyncMock(side_effect=responses)
    return provider


class TestValidationRunner:
    @pytest.mark.asyncio
    async def test_validate_with_explicit_config(self):
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(
                    name="ruff",
                    command="ruff check --output-format json .",
                    timeout=10,
                )
            ],
            max_total_timeout=30,
        )
        # Script execution returns ruff output
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
        provider = _make_provider([script_result])

        runner = ValidationRunner()
        results = await runner.validate("sb1", provider, config=config)

        assert len(results) == 1
        assert results[0].validator == "ruff"
        assert results[0].passed is True
        assert results[0].errors == []

    @pytest.mark.asyncio
    async def test_validate_with_errors(self):
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(
                    name="ruff",
                    command="ruff check --output-format json .",
                )
            ],
        )
        ruff_output = (
            '[{"filename": "a.py", "message": "unused", "code": "F401", '
            '"location": {"row": 1, "column": 1}, "fix": null}]'
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                f"{ruff_output}\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===1===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=1.0,
        )
        provider = _make_provider([script_result])

        runner = ValidationRunner()
        results = await runner.validate("sb1", provider, config=config)

        assert len(results) == 1
        assert results[0].passed is False
        assert len(results[0].errors) == 1
        assert results[0].errors[0].rule == "F401"

    @pytest.mark.asyncio
    async def test_validate_no_validators(self):
        """Empty workspace with no config returns empty results."""
        # First call: cat validators.yaml fails
        yaml_result = CodeExecutionResult(stdout="", stderr="", exit_code=1, execution_time=0.01)
        # Second call: ls for detection shows empty workspace
        ls_result = CodeExecutionResult(
            stdout="README.md\n", stderr="", exit_code=0, execution_time=0.01
        )
        provider = _make_provider([yaml_result, ls_result])

        runner = ValidationRunner()
        results = await runner.validate("sb1", provider)
        assert results == []

    @pytest.mark.asyncio
    async def test_validate_script_execution_failure(self):
        config = ValidationPipelineConfig(
            validators=[ValidatorConfig(name="ruff", command="ruff check .")],
        )
        provider = AsyncMock()
        provider.run_code = AsyncMock(side_effect=RuntimeError("sandbox crashed"))

        runner = ValidationRunner()
        results = await runner.validate("sb1", provider, config=config)

        assert len(results) == 1
        assert results[0].validator == "pipeline"
        assert results[0].passed is False

    @pytest.mark.asyncio
    async def test_validate_auto_detect(self):
        """Auto-detection finds pyproject.toml and suggests ruff/mypy."""
        # First call: cat validators.yaml fails
        yaml_result = CodeExecutionResult(stdout="", stderr="", exit_code=1, execution_time=0.01)
        # Second call: ls shows Python project
        ls_result = CodeExecutionResult(
            stdout="pyproject.toml\nsrc\ntests\n",
            stderr="",
            exit_code=0,
            execution_time=0.01,
        )
        # Third call: combined script execution
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                "[]\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
                "===VALIDATOR_START===mypy===\n"
                "Success: no issues found\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=2.0,
        )
        provider = _make_provider([yaml_result, ls_result, script_result])

        runner = ValidationRunner()
        results = await runner.validate("sb1", provider)
        assert len(results) == 2
        assert results[0].validator == "ruff"
        assert results[1].validator == "mypy"
