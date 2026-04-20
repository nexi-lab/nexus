"""Fixtures for MCP integration tests."""

import os

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
        "NEXUS_PROFILE",
    ]

    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)

    yield


@pytest.fixture
def mcp_http_base_url() -> str:
    """Base URL for the MCP HTTP transport under test.

    Override via MCP_HTTP_URL env var. Default assumes a running nexus
    stack with MCP_TRANSPORT=http on port 8081.
    """
    return os.environ.get("MCP_HTTP_URL", "http://localhost:8081")
