"""Tests for script builder."""

from __future__ import annotations

from nexus.validation.models import ValidatorConfig
from nexus.validation.script_builder import (
    build_simple_validation_script,
    parse_simple_script_output,
)


class TestBuildSimpleValidationScript:
    def test_empty_configs(self):
        script = build_simple_validation_script([])
        assert "NO_VALIDATORS" in script

    def test_single_validator(self):
        config = ValidatorConfig(name="ruff", command="ruff check .", timeout=10)
        script = build_simple_validation_script([config])
        assert "===VALIDATOR_START===ruff===" in script
        assert "===VALIDATOR_END===" in script
        assert "set +e" in script
        assert "timeout 10" in script

    def test_multiple_validators(self):
        configs = [
            ValidatorConfig(name="ruff", command="ruff check ."),
            ValidatorConfig(name="mypy", command="mypy ."),
        ]
        script = build_simple_validation_script(configs)
        assert "===VALIDATOR_START===ruff===" in script
        assert "===VALIDATOR_START===mypy===" in script

    def test_workspace_path(self):
        config = ValidatorConfig(name="ruff", command="ruff check .")
        script = build_simple_validation_script([config], workspace_path="/code")
        assert "/code" in script


class TestParseSimpleScriptOutput:
    def test_parse_single_result(self):
        output = (
            "===VALIDATOR_START===ruff===\n"
            '[{"file": "a.py"}]\n'
            "===VALIDATOR_STDERR===\n"
            "===VALIDATOR_EXIT===0===\n"
            "===VALIDATOR_END===\n"
        )
        results = parse_simple_script_output(output)
        assert len(results) == 1
        assert results[0]["name"] == "ruff"
        assert results[0]["exit_code"] == 0
        assert '[{"file": "a.py"}]' in str(results[0]["stdout"])

    def test_parse_multiple_results(self):
        output = (
            "===VALIDATOR_START===ruff===\n"
            "stdout1\n"
            "===VALIDATOR_STDERR===\n"
            "err1\n"
            "===VALIDATOR_EXIT===0===\n"
            "===VALIDATOR_END===\n"
            "===VALIDATOR_START===mypy===\n"
            "stdout2\n"
            "===VALIDATOR_STDERR===\n"
            "err2\n"
            "===VALIDATOR_EXIT===1===\n"
            "===VALIDATOR_END===\n"
        )
        results = parse_simple_script_output(output)
        assert len(results) == 2
        assert results[0]["name"] == "ruff"
        assert results[0]["exit_code"] == 0
        assert results[1]["name"] == "mypy"
        assert results[1]["exit_code"] == 1

    def test_parse_empty_output(self):
        results = parse_simple_script_output("")
        assert results == []

    def test_parse_invalid_exit_code(self):
        output = (
            "===VALIDATOR_START===ruff===\n"
            "===VALIDATOR_STDERR===\n"
            "===VALIDATOR_EXIT===abc===\n"
            "===VALIDATOR_END===\n"
        )
        results = parse_simple_script_output(output)
        assert results[0]["exit_code"] == 1  # fallback
