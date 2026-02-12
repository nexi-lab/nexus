"""Tests for Reflect REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers the 1 reflect endpoint (POST /api/v2/reflect).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_llm_provider,
    get_nexus_fs,
    get_reflector,
)
from nexus.server.api.v2.routers.reflect import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(mock_nexus_fs, mock_reflector, mock_llm_provider, mock_auth_result):
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_reflector():
        return mock_reflector

    async def _mock_llm():
        return mock_llm_provider

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_reflector] = _mock_reflector
    app.dependency_overrides[get_llm_provider] = _mock_llm
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def app_no_llm(mock_nexus_fs, mock_reflector, mock_auth_result):
    """App without LLM provider."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_reflector():
        return mock_reflector

    async def _no_llm():
        return None

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_reflector] = _mock_reflector
    app.dependency_overrides[get_llm_provider] = _no_llm
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client_no_llm(app_no_llm):
    return TestClient(app_no_llm, raise_server_exceptions=False)


@pytest.fixture
def app_no_auth(mock_nexus_fs, mock_llm_provider):
    """App where auth rejects — no reflector override so nested auth triggers."""
    app = FastAPI()
    app.include_router(router)

    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _mock_llm():
        return mock_llm_provider

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _reject_auth
    app.dependency_overrides[get_llm_provider] = _mock_llm
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client_no_auth(app_no_auth):
    return TestClient(app_no_auth, raise_server_exceptions=False)


# =============================================================================
# Test: POST /api/v2/reflect
# =============================================================================


class TestReflect:
    def test_reflect_success(self, client, mock_reflector):
        response = client.post(
            "/api/v2/reflect",
            json={"trajectory_id": "traj-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["memory_id"] == "mem-ref-123"
        assert data["confidence"] == 0.85
        assert len(data["helpful_strategies"]) == 1

    def test_reflect_with_context(self, client, mock_reflector):
        response = client.post(
            "/api/v2/reflect",
            json={
                "trajectory_id": "traj-123",
                "context": "Debugging session",
                "reflection_prompt": "What went well?",
            },
        )
        assert response.status_code == 200

    def test_reflect_no_llm(self, client_no_llm):
        response = client_no_llm.post(
            "/api/v2/reflect",
            json={"trajectory_id": "traj-123"},
        )
        assert response.status_code == 503
        assert "LLM provider not configured" in response.json()["detail"]

    def test_reflect_not_found(self, client, mock_reflector):
        mock_reflector.reflect_async.side_effect = ValueError("Trajectory not found")
        response = client.post(
            "/api/v2/reflect",
            json={"trajectory_id": "nonexistent"},
        )
        assert response.status_code == 404

    def test_reflect_error(self, client, mock_reflector):
        mock_reflector.reflect_async.side_effect = RuntimeError("LLM error")
        response = client.post(
            "/api/v2/reflect",
            json={"trajectory_id": "traj-123"},
        )
        assert response.status_code == 500
        assert "Failed to perform reflection" in response.json()["detail"]


# =============================================================================
# Test: Auth enforcement
# =============================================================================


class TestAuthEnforcement:
    def test_rejects_unauthenticated(self, client_no_auth):
        response = client_no_auth.post(
            "/api/v2/reflect",
            json={"trajectory_id": "traj-123"},
        )
        assert response.status_code == 401
