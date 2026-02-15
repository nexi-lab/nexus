"""Shared fixtures for validation unit tests."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nexus.sandbox.sandbox_provider import CodeExecutionResult
from nexus.validation.models import ValidatorConfig

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "validation"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


def load_fixture(name: str) -> str:
    """Load a fixture file as string."""
    return (FIXTURES_DIR / name).read_text()


@pytest.fixture
def ruff_errors_json() -> str:
    return load_fixture("ruff_errors.json")


@pytest.fixture
def ruff_clean_json() -> str:
    return load_fixture("ruff_clean.json")


@pytest.fixture
def mypy_errors_txt() -> str:
    return load_fixture("mypy_errors.txt")


@pytest.fixture
def mypy_clean_txt() -> str:
    return load_fixture("mypy_clean.txt")


@pytest.fixture
def eslint_errors_json() -> str:
    return load_fixture("eslint_errors.json")


@pytest.fixture
def eslint_clean_json() -> str:
    return load_fixture("eslint_clean.json")


@pytest.fixture
def clippy_errors_jsonl() -> str:
    return load_fixture("clippy_errors.jsonl")


@pytest.fixture
def clippy_clean_jsonl() -> str:
    return load_fixture("clippy_clean.jsonl")


@pytest.fixture
def ruff_config() -> ValidatorConfig:
    return ValidatorConfig(
        name="ruff",
        command="ruff check --output-format json .",
        timeout=10,
        output_format="json",
    )


@pytest.fixture
def mypy_config() -> ValidatorConfig:
    return ValidatorConfig(
        name="mypy",
        command="mypy --no-error-summary .",
        timeout=15,
        output_format="text",
    )


@pytest.fixture
def mock_sandbox_provider() -> AsyncMock:
    """Create a mock SandboxProvider."""
    provider = AsyncMock()
    provider.run_code = AsyncMock(
        return_value=CodeExecutionResult(
            stdout="",
            stderr="",
            exit_code=0,
            execution_time=0.1,
        )
    )
    return provider
