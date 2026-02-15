"""Tests for validation models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexus.validation.models import (
    ValidationError,
    ValidationPipelineConfig,
    ValidationResult,
    ValidatorConfig,
)


class TestValidationError:
    def test_create_error(self):
        err = ValidationError(
            file="app.py",
            line=10,
            column=5,
            severity="error",
            message="unused import",
            rule="F401",
            fix_available=True,
        )
        assert err.file == "app.py"
        assert err.line == 10
        assert err.severity == "error"
        assert err.rule == "F401"
        assert err.fix_available is True

    def test_defaults(self):
        err = ValidationError(
            file="a.py", line=1, column=1, severity="warning", message="msg"
        )
        assert err.rule is None
        assert err.fix_available is False

    def test_frozen(self):
        err = ValidationError(
            file="a.py", line=1, column=1, severity="info", message="msg"
        )
        with pytest.raises(PydanticValidationError):
            err.line = 2  # type: ignore[misc]


class TestValidationResult:
    def test_passed(self):
        result = ValidationResult(validator="ruff", passed=True)
        assert result.passed is True
        assert result.errors == []
        assert result.duration_ms == 0

    def test_with_errors(self):
        err = ValidationError(
            file="a.py", line=1, column=1, severity="error", message="bad"
        )
        result = ValidationResult(
            validator="mypy", passed=False, errors=[err], duration_ms=150
        )
        assert result.passed is False
        assert len(result.errors) == 1
        assert result.duration_ms == 150


class TestValidatorConfig:
    def test_defaults(self):
        config = ValidatorConfig(name="ruff", command="ruff check .")
        assert config.timeout == 10
        assert config.auto_detect == []
        assert config.output_format == "json"
        assert config.enabled is True

    def test_custom(self):
        config = ValidatorConfig(
            name="mypy",
            command="mypy .",
            timeout=20,
            output_format="text",
            enabled=False,
        )
        assert config.timeout == 20
        assert config.output_format == "text"
        assert config.enabled is False

    def test_name_with_dashes_and_underscores(self):
        config = ValidatorConfig(name="cargo-clippy", command="cargo clippy")
        assert config.name == "cargo-clippy"
        config2 = ValidatorConfig(name="my_lint", command="lint")
        assert config2.name == "my_lint"

    def test_name_rejects_shell_metacharacters(self):
        with pytest.raises(PydanticValidationError):
            ValidatorConfig(name="test'; rm -rf /", command="echo")

    def test_name_rejects_empty(self):
        with pytest.raises(PydanticValidationError):
            ValidatorConfig(name="", command="echo")

    def test_name_rejects_spaces(self):
        with pytest.raises(PydanticValidationError):
            ValidatorConfig(name="bad name", command="echo")

    def test_timeout_valid_range(self):
        config = ValidatorConfig(name="ruff", command="ruff", timeout=1)
        assert config.timeout == 1
        config2 = ValidatorConfig(name="ruff", command="ruff", timeout=300)
        assert config2.timeout == 300

    def test_timeout_rejects_zero(self):
        with pytest.raises(PydanticValidationError):
            ValidatorConfig(name="ruff", command="ruff", timeout=0)

    def test_timeout_rejects_negative(self):
        with pytest.raises(PydanticValidationError):
            ValidatorConfig(name="ruff", command="ruff", timeout=-1)

    def test_timeout_rejects_too_large(self):
        with pytest.raises(PydanticValidationError):
            ValidatorConfig(name="ruff", command="ruff", timeout=301)


class TestValidationPipelineConfig:
    def test_defaults(self):
        config = ValidationPipelineConfig()
        assert config.validators == []
        assert config.auto_run is True
        assert config.max_total_timeout == 30

    def test_with_validators(self):
        v = ValidatorConfig(name="ruff", command="ruff check .")
        config = ValidationPipelineConfig(validators=[v], auto_run=False, max_total_timeout=60)
        assert len(config.validators) == 1
        assert config.auto_run is False
        assert config.max_total_timeout == 60
