"""Pytest configuration for evaluation tests.

This module configures pytest to skip evaluation tests by default.
Evaluation tests require:
- ANTHROPIC_API_KEY environment variable
- Manual trigger with: pytest tests/evaluation -m evaluation
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the evaluation marker."""
    config.addinivalue_line(
        "markers",
        "evaluation: marks tests as LLM evaluation tests (requires ANTHROPIC_API_KEY, "
        "deselect with '-m \"not evaluation\"')",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip evaluation tests unless explicitly requested.

    Evaluation tests are skipped by default because they:
    1. Require ANTHROPIC_API_KEY
    2. Consume LLM tokens (cost)
    3. Take longer than unit tests

    To run evaluation tests:
        pytest tests/evaluation -m evaluation
    """
    # Check if evaluation tests are explicitly requested
    markexpr = config.getoption("-m", default="")
    if "evaluation" in str(markexpr):
        # User explicitly requested evaluation tests
        # Check for API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY environment variable not set")
        return

    # Skip all evaluation tests by default
    skip_evaluation = pytest.mark.skip(
        reason="Evaluation tests are skipped by default. "
        "Run with: pytest tests/evaluation -m evaluation"
    )
    for item in items:
        if "evaluation" in item.keywords:
            item.add_marker(skip_evaluation)
