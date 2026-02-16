"""Tests for manifest REST API endpoints (Issue #1428: 10A).

Covers:
1. GET /api/v2/agents/{agent_id}/manifest: happy path, not found, auth
2. PUT /api/v2/agents/{agent_id}/manifest: validation, ownership, happy path
3. POST /api/v2/agents/{agent_id}/manifest/resolve: resolution, empty, error
4. Ownership checks (403 on wrong owner)
5. Data returned in resolve response (2A)
"""

from __future__ import annotations

import types
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.core.agent_record import AgentRecord, AgentState
from nexus.server.api.v2.routers.manifest import (
    _get_require_auth,
    get_nexus_fs,
    router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_context(user_id: str = "user-001", zone_id: str = "zone-001") -> MagicMock:
    """Create a mock operation context matching the auth_result fixture."""
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.user = user_id
    ctx.zone_id = zone_id
    return ctx


def _make_agent_record(
    agent_id: str = "agent-001",
    owner_id: str = "user-001",
    zone_id: str = "zone-001",
    manifest: tuple[dict[str, Any], ...] = (),
) -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id,
        owner_id=owner_id,
        zone_id=zone_id,
        name="test-agent",
        state=AgentState.UNKNOWN,
        generation=0,
        last_heartbeat=None,
        metadata=types.MappingProxyType({}),
        created_at=datetime.now(),
        updated_at=datetime.now(),
        context_manifest=manifest,
    )


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get.return_value = _make_agent_record()
    registry.update_manifest.return_value = _make_agent_record()
    return registry


@pytest.fixture
def mock_resolver():
    resolver = MagicMock()
    resolver.with_executors.return_value = resolver
    return resolver


@pytest.fixture
def mock_nexus_fs(mock_registry, mock_resolver):
    nexus_fs = MagicMock()
    nexus_fs._agent_registry = mock_registry
    nexus_fs._service_extras = {"manifest_resolver": mock_resolver}
    nexus_fs._memory_api = MagicMock()
    return nexus_fs


@pytest.fixture
def auth_result():
    return {
        "user_id": "user-001",
        "user": "user-001",
        "zone_id": "zone-001",
    }


@pytest.fixture
def app(mock_nexus_fs, auth_result):
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return auth_result

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs
    return app


@pytest.fixture
def client(app):
    with patch(
        "nexus.server.api.v2.routers.manifest._get_operation_context",
        return_value=_make_mock_context("user-001"),
    ):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/v2/agents/{agent_id}/manifest
# ---------------------------------------------------------------------------


class TestGetManifest:
    def test_happy_path(self, client: TestClient, mock_registry: MagicMock) -> None:
        """GET returns the agent's current manifest."""
        manifest_data = ({"type": "file_glob", "pattern": "*.py", "required": True},)
        mock_registry.get.return_value = _make_agent_record(manifest=manifest_data)

        resp = client.get("/api/v2/agents/agent-001/manifest")

        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-001"
        assert data["source_count"] == 1
        assert data["sources"][0]["type"] == "file_glob"

    def test_empty_manifest(self, client: TestClient) -> None:
        """GET returns empty sources list for agent with no manifest."""
        resp = client.get("/api/v2/agents/agent-001/manifest")

        assert resp.status_code == 200
        assert resp.json()["source_count"] == 0
        assert resp.json()["sources"] == []

    def test_agent_not_found(self, client: TestClient, mock_registry: MagicMock) -> None:
        """GET returns 404 for unknown agent."""
        mock_registry.get.return_value = None

        resp = client.get("/api/v2/agents/unknown/manifest")

        assert resp.status_code == 404

    def test_wrong_owner(self, client: TestClient, mock_registry: MagicMock) -> None:
        """GET returns 403 when authenticated user doesn't own the agent."""
        mock_registry.get.return_value = _make_agent_record(owner_id="other-user")

        resp = client.get("/api/v2/agents/agent-001/manifest")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v2/agents/{agent_id}/manifest
# ---------------------------------------------------------------------------


class TestSetManifest:
    def test_happy_path(self, client: TestClient, mock_registry: MagicMock) -> None:
        """PUT replaces the manifest with validated sources."""
        manifest_data = (
            {
                "type": "file_glob",
                "pattern": "src/**/*.py",
                "required": True,
                "timeout_seconds": 30.0,
                "max_result_bytes": 1048576,
                "max_files": 50,
            },
        )
        mock_registry.update_manifest.return_value = _make_agent_record(manifest=manifest_data)

        resp = client.put(
            "/api/v2/agents/agent-001/manifest",
            json={"sources": [{"type": "file_glob", "pattern": "src/**/*.py"}]},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 1
        mock_registry.update_manifest.assert_called_once()

    def test_invalid_source_rejected(self, client: TestClient) -> None:
        """PUT rejects invalid source definitions with 422."""
        resp = client.put(
            "/api/v2/agents/agent-001/manifest",
            json={"sources": [{"type": "invalid_type", "foo": "bar"}]},
        )

        assert resp.status_code == 422

    def test_agent_not_found(self, client: TestClient, mock_registry: MagicMock) -> None:
        """PUT returns 404 for unknown agent."""
        mock_registry.get.return_value = None

        resp = client.put(
            "/api/v2/agents/agent-001/manifest",
            json={"sources": []},
        )

        assert resp.status_code == 404

    def test_wrong_owner(self, client: TestClient, mock_registry: MagicMock) -> None:
        """PUT returns 403 when authenticated user doesn't own the agent."""
        mock_registry.get.return_value = _make_agent_record(owner_id="other-user")

        resp = client.put(
            "/api/v2/agents/agent-001/manifest",
            json={"sources": []},
        )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v2/agents/{agent_id}/manifest/resolve
# ---------------------------------------------------------------------------


class TestResolveManifest:
    def test_empty_manifest_returns_empty(self, client: TestClient) -> None:
        """POST resolve with empty manifest returns immediately."""
        resp = client.post("/api/v2/agents/agent-001/manifest/resolve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 0
        assert data["sources"] == []

    def test_resolve_calls_resolver(
        self,
        client: TestClient,
        mock_registry: MagicMock,
        mock_resolver: MagicMock,
    ) -> None:
        """POST resolve invokes the resolver and returns results."""
        from nexus.services.context_manifest.models import ManifestResult, SourceResult

        manifest_data = (
            {
                "type": "file_glob",
                "pattern": "*.py",
                "required": True,
                "timeout_seconds": 30.0,
                "max_result_bytes": 1048576,
                "max_files": 50,
            },
        )
        mock_registry.get.return_value = _make_agent_record(manifest=manifest_data)

        # Mock resolver.resolve to return a ManifestResult
        mock_result = ManifestResult(
            sources=(
                SourceResult.ok(
                    source_type="file_glob",
                    source_name="*.py",
                    data={"files": {"app.py": "print('hi')"}},
                    elapsed_ms=5.0,
                ),
            ),
            resolved_at="2025-01-15T10:00:00",
            total_ms=5.0,
        )
        mock_resolver.resolve = AsyncMock(return_value=mock_result)

        resp = client.post("/api/v2/agents/agent-001/manifest/resolve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 1
        assert data["sources"][0]["status"] == "ok"
        assert data["total_ms"] == 5.0
        # Verify resolved data is included (2A)
        assert data["data"] is not None
        assert len(data["data"]) == 1
        assert data["data"][0]["source_type"] == "file_glob"
        assert data["data"][0]["data"]["files"]["app.py"] == "print('hi')"

    def test_resolve_error_returns_500(
        self,
        client: TestClient,
        mock_registry: MagicMock,
        mock_resolver: MagicMock,
    ) -> None:
        """POST resolve with required source failure returns 500."""
        from nexus.services.context_manifest.models import (
            ManifestResolutionError,
            SourceResult,
        )

        manifest_data = (
            {
                "type": "file_glob",
                "pattern": "*.py",
                "required": True,
                "timeout_seconds": 30.0,
                "max_result_bytes": 1048576,
                "max_files": 50,
            },
        )
        mock_registry.get.return_value = _make_agent_record(manifest=manifest_data)

        # Mock resolver to raise ManifestResolutionError
        failed = SourceResult.error(
            source_type="file_glob",
            source_name="*.py",
            error_message="Workspace not found",
        )
        mock_resolver.resolve = AsyncMock(
            side_effect=ManifestResolutionError(failed_sources=(failed,))
        )

        resp = client.post("/api/v2/agents/agent-001/manifest/resolve")

        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "failed_sources" in detail

    def test_agent_not_found(self, client: TestClient, mock_registry: MagicMock) -> None:
        """POST resolve returns 404 for unknown agent."""
        mock_registry.get.return_value = None

        resp = client.post("/api/v2/agents/unknown/manifest/resolve")

        assert resp.status_code == 404

    def test_wrong_owner(self, client: TestClient, mock_registry: MagicMock) -> None:
        """POST resolve returns 403 when wrong owner."""
        mock_registry.get.return_value = _make_agent_record(owner_id="other-user")

        resp = client.post("/api/v2/agents/agent-001/manifest/resolve")

        assert resp.status_code == 403
