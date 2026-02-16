"""Fixtures for MCP integration tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_mcp_integration_tests(monkeypatch):
    """Isolate MCP integration tests from environment pollution.

    This fixture clears NEXUS environment variables that could
    affect the test configuration and cause intermittent failures.
    """
    # Clear all NEXUS environment variables
    env_vars_to_clear = [
        "NEXUS_BACKEND",
        "NEXUS_DATA_DIR",
        "NEXUS_GCS_BUCKET_NAME",
        "NEXUS_GCS_PROJECT_ID",
        "NEXUS_DATABASE_URL",
        "NEXUS_URL",
        "NEXUS_API_KEY",
        "NEXUS_MODE",
    ]

    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)

    yield
