"""Local validation pipeline for shift-left linting in sandboxes.

Runs heuristic-based linters locally before CI access, returning structured
feedback to agents for self-correction. Inspired by Stripe's Minions pattern.

Usage:
    from nexus.validation import ValidationRunner, ValidationResult

    runner = ValidationRunner()
    results = await runner.validate(sandbox_id, provider)
"""

from nexus.validation.config import ValidatorConfigLoader
from nexus.validation.detector import detect_project_validators
from nexus.validation.models import (
    ValidationError,
    ValidationPipelineConfig,
    ValidationResult,
    ValidatorConfig,
)
from nexus.validation.runner import ValidationRunner

__all__ = [
    "ValidationError",
    "ValidationPipelineConfig",
    "ValidationResult",
    "ValidationRunner",
    "ValidatorConfig",
    "ValidatorConfigLoader",
    "detect_project_validators",
]
