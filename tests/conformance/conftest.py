"""Fixtures for Exchange Protocol conformance tests.

Provides a test server and authentication tokens for schemathesis tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Path to the OpenAPI spec
OPENAPI_SPEC_PATH = (
    Path(__file__).parent.parent.parent / "docs" / "protocol" / "nexus-exchange-v1.openapi.yaml"
)


@pytest.fixture(scope="session")
def openapi_spec_path() -> Path:
    """Return the path to the OpenAPI spec file."""
    if not OPENAPI_SPEC_PATH.exists():
        pytest.skip(f"OpenAPI spec not found at {OPENAPI_SPEC_PATH}")
    return OPENAPI_SPEC_PATH


@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the base URL for conformance testing.

    Set NEXUS_CONFORMANCE_URL to test against a running server.
    Defaults to localhost:2026 for local development.
    """
    return os.environ.get("NEXUS_CONFORMANCE_URL", "http://localhost:2026")


@pytest.fixture(scope="session")
def auth_token() -> str | None:
    """Return an auth token for authenticated endpoints.

    Set NEXUS_CONFORMANCE_TOKEN to provide authentication.
    """
    return os.environ.get("NEXUS_CONFORMANCE_TOKEN")
