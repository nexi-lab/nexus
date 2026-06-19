"""Shared fixtures for surface-coverage tests."""

from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root (4 levels up from this test file)."""
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    """Path to a temp YAML file."""
    return tmp_path / "coverage.yaml"
