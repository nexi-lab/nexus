"""RPC dispatch integration tests for sandbox_validate endpoint.

Verifies that sandbox_validate is properly registered in the RPC dispatch
pipeline: protocol params class, method exposure, and parameter parsing.
No Docker or live server required.
"""

from __future__ import annotations

import dataclasses

from nexus.server.protocol import METHOD_PARAMS, SandboxValidateParams
from nexus.validation.models import (
    ValidationError,
    ValidationResult,
    ValidatorConfig,
)
from nexus.validation.script_builder import (
    build_simple_validation_script,
    parse_simple_script_output,
)


class TestSandboxValidateProtocol:
    """Verify sandbox_validate is properly wired into the RPC protocol."""

    def test_registered_in_method_params(self):
        assert "sandbox_validate" in METHOD_PARAMS
        assert METHOD_PARAMS["sandbox_validate"] is SandboxValidateParams

    def test_params_class_fields(self):
        fields = {f.name for f in dataclasses.fields(SandboxValidateParams)}
        assert "sandbox_id" in fields
        assert "workspace_path" in fields
        assert "context" in fields

    def test_params_defaults(self):
        params = SandboxValidateParams(sandbox_id="sb-123")
        assert params.workspace_path == "/workspace"
        assert params.context is None

    def test_params_custom(self):
        params = SandboxValidateParams(
            sandbox_id="sb-456",
            workspace_path="/code",
            context={"user_id": "u1"},
        )
        assert params.sandbox_id == "sb-456"
        assert params.workspace_path == "/code"


class TestValidationResultSerialization:
    """Verify results serialize cleanly for JSON-RPC response."""

    def test_single_result_serialization(self):
        result = ValidationResult(
            validator="ruff",
            passed=False,
            errors=[
                ValidationError(
                    file="app.py",
                    line=10,
                    column=5,
                    severity="error",
                    message="unused import",
                    rule="F401",
                    fix_available=True,
                )
            ],
            duration_ms=150,
        )
        serialized = result.model_dump()
        assert serialized["validator"] == "ruff"
        assert serialized["passed"] is False
        assert len(serialized["errors"]) == 1
        assert serialized["errors"][0]["file"] == "app.py"
        assert serialized["errors"][0]["severity"] == "error"
        assert serialized["errors"][0]["fix_available"] is True

    def test_empty_result_serialization(self):
        result = ValidationResult(validator="mypy", passed=True)
        serialized = result.model_dump()
        assert serialized["errors"] == []
        assert serialized["duration_ms"] == 0

    def test_response_envelope(self):
        """Validate the response format matches sandbox_validate return."""
        results = [
            ValidationResult(validator="ruff", passed=True),
            ValidationResult(
                validator="mypy",
                passed=False,
                errors=[
                    ValidationError(
                        file="x.py", line=1, column=1, severity="error", message="bad type"
                    )
                ],
            ),
        ]
        response = {"validations": [r.model_dump() for r in results]}
        assert len(response["validations"]) == 2
        assert response["validations"][0]["validator"] == "ruff"
        assert response["validations"][1]["passed"] is False


class TestCodeExecutionResultValidations:
    """Verify CodeExecutionResult carries validations correctly."""

    def test_default_no_validations(self):
        from nexus.sandbox.sandbox_provider import CodeExecutionResult

        result = CodeExecutionResult(stdout="", stderr="", exit_code=0, execution_time=0.1)
        assert result.validations is None

    def test_with_validations(self):
        from nexus.sandbox.sandbox_provider import CodeExecutionResult

        v = ValidationResult(validator="ruff", passed=True)
        result = CodeExecutionResult(
            stdout="hello", stderr="", exit_code=0, execution_time=0.5, validations=[v]
        )
        assert result.validations is not None
        assert len(result.validations) == 1
        assert result.validations[0].validator == "ruff"

    def test_dataclass_asdict_with_validations(self):
        """Verify dataclasses.asdict works for RPC serialization."""
        from nexus.sandbox.sandbox_provider import CodeExecutionResult

        result = CodeExecutionResult(stdout="out", stderr="err", exit_code=0, execution_time=0.1)
        d = dataclasses.asdict(result)
        assert d["stdout"] == "out"
        assert d["validations"] is None


class TestPerformanceCharacteristics:
    """Verify the pipeline has no N+1 patterns and stays within budget."""

    def test_single_script_for_multiple_validators(self):
        """All validators run via a single bash script — no N+1."""
        configs = [
            ValidatorConfig(name="ruff", command="ruff check ."),
            ValidatorConfig(name="mypy", command="mypy ."),
            ValidatorConfig(name="eslint", command="npx eslint ."),
        ]
        script = build_simple_validation_script(configs)
        # Should be one script with all three
        assert script.count("===VALIDATOR_START===") == 3
        assert "set +e" in script

    def test_parse_overhead_minimal(self):
        """Parsing structured output is O(n) in output lines."""
        import time

        # Generate large output (1000 lines per validator)
        big_output = ""
        for i in range(10):
            big_output += f"===VALIDATOR_START===v{i}===\n"
            for j in range(100):
                big_output += f"line {j} of validator {i}\n"
            big_output += "===VALIDATOR_STDERR===\n"
            big_output += f"stderr for v{i}\n"
            big_output += "===VALIDATOR_EXIT===0===\n"
            big_output += "===VALIDATOR_END===\n"

        start = time.monotonic()
        results = parse_simple_script_output(big_output)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert len(results) == 10
        # Parsing 1000+ lines should take <50ms
        assert elapsed_ms < 50, f"Parsing took {elapsed_ms:.1f}ms — too slow"

    def test_config_caching(self):
        """Config loader caches by key — second load is instant."""
        from nexus.validation.config import ValidatorConfigLoader

        loader = ValidatorConfigLoader()
        yaml = "validators:\n  - name: ruff\n    command: ruff check .\n"

        c1 = loader.load_from_string(yaml, cache_key="test")
        c2 = loader.load_from_string(yaml, cache_key="test")
        assert c1 is c2  # Same object from cache

    def test_detection_single_ls_call(self):
        """Detection uses exactly one ls command."""
        from unittest.mock import AsyncMock

        from nexus.sandbox.sandbox_provider import CodeExecutionResult
        from nexus.validation.detector import detect_project_validators

        provider = AsyncMock()
        provider.run_code = AsyncMock(
            return_value=CodeExecutionResult(
                stdout="pyproject.toml\n", stderr="", exit_code=0, execution_time=0.01
            )
        )

        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            detect_project_validators("sb1", provider)
        )
        assert provider.run_code.call_count == 1  # Single ls call
        assert "ruff" in result
