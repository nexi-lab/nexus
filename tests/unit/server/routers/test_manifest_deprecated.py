"""Tests for deprecated manifest REST endpoints returning 410 Gone (Issue #2984).

Verifies that all 3 manifest endpoints return 410 with deprecation message
directing callers to the nexus_resolve_context MCP tool.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.manifest import router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestManifestDeprecatedEndpoints:
    def test_get_manifest_returns_410(self, client: TestClient) -> None:
        """GET /api/v2/agents/{id}/manifest returns 410 Gone."""
        response = client.get("/api/v2/agents/test-agent/manifest")
        assert response.status_code == 410
        body = response.json()
        assert "deprecated" in body["message"].lower()
        assert "nexus_resolve_context" in body["message"]

    def test_put_manifest_returns_410(self, client: TestClient) -> None:
        """PUT /api/v2/agents/{id}/manifest returns 410 Gone."""
        response = client.put(
            "/api/v2/agents/test-agent/manifest",
            json={"sources": []},
        )
        assert response.status_code == 410
        body = response.json()
        assert "deprecated" in body["message"].lower()

    def test_resolve_manifest_returns_410(self, client: TestClient) -> None:
        """POST /api/v2/agents/{id}/manifest/resolve returns 410 Gone."""
        response = client.post("/api/v2/agents/test-agent/manifest/resolve")
        assert response.status_code == 410
        body = response.json()
        assert "nexus_resolve_context" in body["message"]
        assert "migration" in body
