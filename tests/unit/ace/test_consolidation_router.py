"""Tests for Consolidation REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers all 4 consolidation endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_consolidation_engine,
    get_hierarchy_manager,
    get_llm_provider,
    get_memory_api,
    get_nexus_fs,
)
from nexus.server.api.v2.routers.consolidation import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(
    mock_nexus_fs,
    mock_consolidation_engine,
    mock_hierarchy_manager,
    mock_llm_provider,
    mock_memory_api,
    mock_auth_result,
):
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_engine():
        return mock_consolidation_engine

    async def _mock_hierarchy():
        return mock_hierarchy_manager

    async def _mock_llm():
        return mock_llm_provider

    async def _mock_memory():
        return mock_memory_api

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_consolidation_engine] = _mock_engine
    app.dependency_overrides[get_hierarchy_manager] = _mock_hierarchy
    app.dependency_overrides[get_llm_provider] = _mock_llm
    app.dependency_overrides[get_memory_api] = _mock_memory
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def app_no_llm(
    mock_nexus_fs,
    mock_consolidation_engine,
    mock_hierarchy_manager,
    mock_memory_api,
    mock_auth_result,
):
    """App without LLM provider for hierarchy tests."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_engine():
        return mock_consolidation_engine

    async def _mock_hierarchy():
        return mock_hierarchy_manager

    async def _no_llm():
        return None

    async def _mock_memory():
        return mock_memory_api

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_consolidation_engine] = _mock_engine
    app.dependency_overrides[get_hierarchy_manager] = _mock_hierarchy
    app.dependency_overrides[get_llm_provider] = _no_llm
    app.dependency_overrides[get_memory_api] = _mock_memory
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client_no_llm(app_no_llm):
    return TestClient(app_no_llm, raise_server_exceptions=False)


@pytest.fixture
def app_no_auth(mock_nexus_fs, mock_memory_api):
    """App where auth rejects — no engine/hierarchy override so nested auth triggers."""
    app = FastAPI()
    app.include_router(router)

    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _mock_memory():
        return mock_memory_api

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _reject_auth
    app.dependency_overrides[get_memory_api] = _mock_memory
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client_no_auth(app_no_auth):
    return TestClient(app_no_auth, raise_server_exceptions=False)


# =============================================================================
# Test: POST /api/v2/consolidate (affinity)
# =============================================================================


class TestConsolidateByAffinity:
    def test_consolidate_success(self, client, mock_consolidation_engine):
        response = client.post(
            "/api/v2/consolidate",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["clusters_formed"] == 1
        assert data["total_consolidated"] == 2

    def test_consolidate_with_params(self, client, mock_consolidation_engine):
        response = client.post(
            "/api/v2/consolidate",
            json={
                "memory_ids": ["m1", "m2", "m3"],
                "beta": 0.8,
                "lambda_decay": 0.05,
                "affinity_threshold": 0.9,
                "importance_max": 0.3,
                "memory_type": "fact",
                "namespace": "test",
                "limit": 50,
            },
        )
        assert response.status_code == 200

    def test_consolidate_error(self, client, mock_consolidation_engine):
        mock_consolidation_engine.consolidate_by_affinity_async.side_effect = RuntimeError(
            "LLM error"
        )
        response = client.post("/api/v2/consolidate", json={})
        assert response.status_code == 500
        assert "Failed to consolidate memories" in response.json()["detail"]


# =============================================================================
# Test: POST /api/v2/consolidate/hierarchy
# =============================================================================


class TestBuildHierarchy:
    def test_hierarchy_success(self, client, mock_hierarchy_manager):
        response = client.post(
            "/api/v2/consolidate/hierarchy",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_memories"] == 10
        assert data["total_abstracts_created"] == 3

    def test_hierarchy_no_llm(self, client_no_llm):
        response = client_no_llm.post(
            "/api/v2/consolidate/hierarchy",
            json={},
        )
        assert response.status_code == 503
        assert "LLM provider not configured" in response.json()["detail"]

    def test_hierarchy_with_params(self, client, mock_hierarchy_manager):
        response = client.post(
            "/api/v2/consolidate/hierarchy",
            json={
                "memory_ids": ["m1", "m2"],
                "max_levels": 5,
                "cluster_threshold": 0.8,
                "beta": 0.6,
                "lambda_decay": 0.2,
                "time_unit_hours": 12.0,
            },
        )
        assert response.status_code == 200

    def test_hierarchy_error(self, client, mock_hierarchy_manager):
        mock_hierarchy_manager.build_hierarchy_async.side_effect = RuntimeError("Error")
        response = client.post("/api/v2/consolidate/hierarchy", json={})
        assert response.status_code == 500


# =============================================================================
# Test: GET /api/v2/consolidate/hierarchy/{id}
# =============================================================================


class TestGetHierarchy:
    def test_get_success(self, client, mock_hierarchy_manager):
        response = client.get("/api/v2/consolidate/hierarchy/mem-123")
        assert response.status_code == 200
        assert "hierarchy" in response.json()

    def test_get_not_found(self, client, mock_hierarchy_manager):
        mock_hierarchy_manager.get_hierarchy_for_memory.side_effect = ValueError("Not found")
        response = client.get("/api/v2/consolidate/hierarchy/nonexistent")
        assert response.status_code == 404

    def test_get_error(self, client, mock_hierarchy_manager):
        mock_hierarchy_manager.get_hierarchy_for_memory.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/consolidate/hierarchy/mem-123")
        assert response.status_code == 500


# =============================================================================
# Test: POST /api/v2/consolidate/decay
# =============================================================================


class TestApplyDecay:
    def test_decay_success(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/consolidate/decay",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["updated"] == 10

    def test_decay_with_params(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/consolidate/decay",
            json={
                "decay_factor": 0.9,
                "min_importance": 0.05,
                "batch_size": 500,
            },
        )
        assert response.status_code == 200

    def test_decay_error(self, client, mock_memory_api):
        mock_memory_api.apply_decay_batch.side_effect = RuntimeError("DB error")
        response = client.post("/api/v2/consolidate/decay", json={})
        assert response.status_code == 500
        assert "Failed to apply decay" in response.json()["detail"]


# =============================================================================
# Test: Auth enforcement (parametrized)
# =============================================================================


class TestAuthEnforcement:
    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("POST", "/api/v2/consolidate", {}),
            ("POST", "/api/v2/consolidate/hierarchy", {}),
            ("GET", "/api/v2/consolidate/hierarchy/mem-1", None),
            ("POST", "/api/v2/consolidate/decay", {}),
        ],
    )
    def test_rejects_unauthenticated(self, client_no_auth, method, path, json_body):
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        response = getattr(client_no_auth, method.lower())(path, **kwargs)
        assert response.status_code == 401
