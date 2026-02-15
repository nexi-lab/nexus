"""Integration tests for the full validation pipeline with mock sandbox."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.sandbox.sandbox_provider import CodeExecutionResult
from nexus.validation.models import ValidationPipelineConfig, ValidatorConfig
from nexus.validation.runner import ValidationRunner


def _make_provider(*responses: CodeExecutionResult) -> AsyncMock:
    provider = AsyncMock()
    provider.run_code = AsyncMock(side_effect=list(responses))
    return provider


class TestValidationPipelineIntegration:
    """Full pipeline integration tests using mock sandbox provider."""

    @pytest.mark.asyncio
    async def test_full_python_pipeline(self):
        """End-to-end: detect Python project → run ruff+mypy → parse results."""
        # cat validators.yaml: not found
        yaml_result = CodeExecutionResult(
            stdout="", stderr="No such file", exit_code=1, execution_time=0.01
        )
        # ls: Python project detected
        ls_result = CodeExecutionResult(
            stdout="pyproject.toml\nsrc\ntests\nREADME.md\n",
            stderr="",
            exit_code=0,
            execution_time=0.05,
        )
        # Script execution: ruff finds issues, mypy clean
        ruff_output = (
            '[{"filename": "src/app.py", "message": "unused import", '
            '"code": "F401", "location": {"row": 1, "column": 8}, '
            '"fix": {"message": "Remove"}}]'
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                f"{ruff_output}\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===1===\n"
                "===VALIDATOR_END===\n"
                "===VALIDATOR_START===mypy===\n"
                "Success: no issues found in 3 source files\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=3.5,
        )
        provider = _make_provider(yaml_result, ls_result, script_result)

        runner = ValidationRunner()
        results = await runner.validate("test-sb", provider)

        assert len(results) == 2
        # Ruff found issues
        ruff_result = results[0]
        assert ruff_result.validator == "ruff"
        assert ruff_result.passed is False
        assert len(ruff_result.errors) == 1
        assert ruff_result.errors[0].rule == "F401"
        assert ruff_result.errors[0].fix_available is True
        # Mypy clean
        mypy_result = results[1]
        assert mypy_result.validator == "mypy"
        assert mypy_result.passed is True

    @pytest.mark.asyncio
    async def test_yaml_config_override(self):
        """validators.yaml in workspace overrides auto-detection."""
        yaml_content = (
            "validators:\n"
            "  - name: ruff\n"
            "    command: ruff check --output-format json src/\n"
            "    timeout: 5\n"
        )
        yaml_result = CodeExecutionResult(
            stdout=yaml_content, stderr="", exit_code=0, execution_time=0.01
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
            execution_time=1.0,
        )
        provider = _make_provider(yaml_result, script_result)

        runner = ValidationRunner()
        results = await runner.validate("test-sb", provider)

        assert len(results) == 1
        assert results[0].validator == "ruff"
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_explicit_config_skips_detection(self):
        """Passing explicit config skips both YAML load and detection."""
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(name="eslint", command="npx eslint --format json .")
            ],
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===eslint===\n"
                "[]\n"
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===0===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=2.0,
        )
        provider = _make_provider(script_result)

        runner = ValidationRunner()
        results = await runner.validate("test-sb", provider, config=config)

        assert len(results) == 1
        assert results[0].validator == "eslint"
        # Provider was only called once (script execution, no YAML/detection)
        assert provider.run_code.call_count == 1

    @pytest.mark.asyncio
    async def test_disabled_validators_skipped(self):
        """Validators with enabled=false are not executed."""
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(name="ruff", command="ruff check .", enabled=True),
                ValidatorConfig(name="mypy", command="mypy .", enabled=False),
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
        provider = _make_provider(script_result)

        runner = ValidationRunner()
        results = await runner.validate("test-sb", provider, config=config)

        # Only ruff should run
        assert len(results) == 1
        assert results[0].validator == "ruff"

    @pytest.mark.asyncio
    async def test_result_serialization(self):
        """ValidationResult serializes cleanly to dict."""
        config = ValidationPipelineConfig(
            validators=[
                ValidatorConfig(name="ruff", command="ruff check --output-format json .")
            ],
        )
        script_result = CodeExecutionResult(
            stdout=(
                "===VALIDATOR_START===ruff===\n"
                '[{"filename": "a.py", "message": "bad", "code": "E1", '
                '"location": {"row": 1, "column": 1}, "fix": null}]\n'
                "===VALIDATOR_STDERR===\n"
                "===VALIDATOR_EXIT===1===\n"
                "===VALIDATOR_END===\n"
            ),
            stderr="",
            exit_code=0,
            execution_time=1.0,
        )
        provider = _make_provider(script_result)

        runner = ValidationRunner()
        results = await runner.validate("test-sb", provider, config=config)

        # Serialize to dict for API response
        serialized = [r.model_dump() for r in results]
        assert len(serialized) == 1
        assert serialized[0]["validator"] == "ruff"
        assert serialized[0]["passed"] is False
        assert isinstance(serialized[0]["errors"], list)
        assert serialized[0]["errors"][0]["file"] == "a.py"
