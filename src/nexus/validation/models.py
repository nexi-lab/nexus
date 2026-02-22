"""Validation pipeline data models.

Pydantic models for structured validation results, errors, and configuration.
All models are immutable (frozen) to prevent accidental mutation.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Validator names must be alphanumeric with dashes/underscores only.
_VALIDATOR_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class ValidationError(BaseModel, frozen=True):
    """A single validation finding from a linter/checker."""

    file: str
    line: int
    column: int
    severity: Literal["error", "warning", "info"]
    message: str
    rule: str | None = None
    fix_available: bool = False


class ValidationResult(BaseModel, frozen=True):
    """Result from running a single validator."""

    validator: str
    passed: bool
    errors: list[ValidationError] = Field(default_factory=list)
    duration_ms: int = 0


class ValidatorConfig(BaseModel, frozen=True):
    """Configuration for a single validator tool."""

    name: str
    command: str
    timeout: int = 10
    auto_detect: list[str] = Field(default_factory=list)
    output_format: Literal["json", "text"] = "json"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _VALIDATOR_NAME_PATTERN.match(v):
            raise ValueError(f"Validator name must be alphanumeric/dash/underscore: {v!r}")
        return v

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        if v < 1 or v > 300:
            raise ValueError(f"Timeout must be between 1 and 300 seconds, got {v}")
        return v


class ValidationPipelineConfig(BaseModel, frozen=True):
    """Top-level pipeline configuration."""

    validators: list[ValidatorConfig] = Field(default_factory=list)
    auto_run: bool = True
    max_total_timeout: int = 30
