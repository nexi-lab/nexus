"""Tests for Memory REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers all 14 memory endpoints (stats, CRUD, search, query, batch,
version history, rollback, diff, lineage).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_memory_api,
    get_nexus_fs,
)
from nexus.server.api.v2.routers.memories import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(mock_nexus_fs, mock_memory_api, mock_auth_result):
    """Create test FastAPI app with memories router."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_memory_api():
        return mock_memory_api

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_memory_api] = _mock_memory_api
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client(app):
    """Test client."""
    return TestClient(app)


@pytest.fixture
def app_no_auth(mock_nexus_fs, mock_memory_api):
    """App where auth rejects all requests."""
    app = FastAPI()
    app.include_router(router)

    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    async def _mock_memory_api_dep():
        return mock_memory_api

    app.dependency_overrides[_get_require_auth()] = _reject_auth
    app.dependency_overrides[get_memory_api] = _mock_memory_api_dep

    return app


@pytest.fixture
def client_no_auth(app_no_auth):
    """Client without auth."""
    return TestClient(app_no_auth, raise_server_exceptions=False)


# =============================================================================
# Test: POST /api/v2/memories (store)
# =============================================================================


class TestStoreMemory:
    def test_store_success(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories",
            json={"content": "test memory", "scope": "user"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["memory_id"] == "mem-123"
        assert data["status"] == "created"

    def test_store_dict_content(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories",
            json={"content": {"key": "value"}, "scope": "user"},
        )
        assert response.status_code == 201

    def test_store_with_all_fields(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories",
            json={
                "content": "full test",
                "scope": "agent",
                "memory_type": "fact",
                "importance": 0.9,
                "namespace": "test/ns",
                "path_key": "my-key",
                "state": "active",
                "extract_entities": True,
                "extract_temporal": False,
                "extract_relationships": True,
                "store_to_graph": True,
                "valid_at": "2025-01-01T00:00:00",
                "metadata": {"source": "test"},
            },
        )
        assert response.status_code == 201

    def test_store_error(self, client, mock_memory_api):
        mock_memory_api.store.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/memories",
            json={"content": "fail", "scope": "user"},
        )
        assert response.status_code == 500
        assert "Failed to store memory" in response.json()["detail"]


# =============================================================================
# Test: GET /api/v2/memories/{id}
# =============================================================================


class TestGetMemory:
    def test_get_success(self, client, mock_memory_api):
        response = client.get("/api/v2/memories/mem-123")
        assert response.status_code == 200
        assert response.json()["memory"]["memory_id"] == "mem-123"

    def test_get_not_found(self, client, mock_memory_api):
        mock_memory_api.get.return_value = None
        response = client.get("/api/v2/memories/nonexistent")
        assert response.status_code == 404

    def test_get_error(self, client, mock_memory_api):
        mock_memory_api.get.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/memories/mem-123")
        assert response.status_code == 500
        assert "Failed to retrieve memory" in response.json()["detail"]


# =============================================================================
# Test: PUT /api/v2/memories/{id}
# =============================================================================


class TestUpdateMemory:
    def test_update_success(self, client, mock_memory_api):
        mock_memory_api.store.return_value = "mem-456"
        response = client.put(
            "/api/v2/memories/mem-123",
            json={"content": "updated content"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["memory_id"] == "mem-456"
        assert data["status"] == "updated"

    def test_update_not_found(self, client, mock_memory_api):
        mock_memory_api.get.return_value = None
        response = client.put(
            "/api/v2/memories/nonexistent",
            json={"content": "updated"},
        )
        assert response.status_code == 404

    def test_update_calls_ensure_upsert_key(self, client, mock_memory_api):
        client.put(
            "/api/v2/memories/mem-123",
            json={"content": "updated"},
        )
        mock_memory_api.ensure_upsert_key.assert_called_once()


# =============================================================================
# Test: DELETE /api/v2/memories/{id}
# =============================================================================


class TestDeleteMemory:
    def test_delete_success(self, client, mock_memory_api):
        response = client.delete("/api/v2/memories/mem-123")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

    def test_delete_not_found(self, client, mock_memory_api):
        mock_memory_api.delete.return_value = False
        response = client.delete("/api/v2/memories/nonexistent")
        assert response.status_code == 404


# =============================================================================
# Test: POST /api/v2/memories/{id}/invalidate
# =============================================================================


class TestInvalidateMemory:
    def test_invalidate_success(self, client, mock_memory_api):
        response = client.post("/api/v2/memories/mem-123/invalidate")
        assert response.status_code == 200
        assert response.json()["invalidated"] is True

    def test_invalidate_not_found(self, client, mock_memory_api):
        mock_memory_api.invalidate.return_value = False
        response = client.post("/api/v2/memories/nonexistent/invalidate")
        assert response.status_code == 404

    def test_invalidate_with_timestamp(self, client, mock_memory_api):
        response = client.post("/api/v2/memories/mem-123/invalidate?invalid_at=2025-06-01T00:00:00")
        assert response.status_code == 200
        assert response.json()["invalid_at"] == "2025-06-01T00:00:00"


# =============================================================================
# Test: POST /api/v2/memories/{id}/revalidate
# =============================================================================


class TestRevalidateMemory:
    def test_revalidate_success(self, client, mock_memory_api):
        response = client.post("/api/v2/memories/mem-123/revalidate")
        assert response.status_code == 200
        assert response.json()["revalidated"] is True

    def test_revalidate_not_found(self, client, mock_memory_api):
        mock_memory_api.revalidate.return_value = False
        response = client.post("/api/v2/memories/nonexistent/revalidate")
        assert response.status_code == 404


# =============================================================================
# Test: POST /api/v2/memories/search
# =============================================================================


class TestSearchMemories:
    def test_search_success(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories/search",
            json={"query": "test query"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["search_mode"] == "hybrid"

    def test_search_with_filters(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories/search",
            json={
                "query": "test",
                "scope": "user",
                "memory_type": "fact",
                "limit": 5,
                "search_mode": "semantic",
            },
        )
        assert response.status_code == 200

    def test_search_error(self, client, mock_memory_api):
        mock_memory_api.search.side_effect = RuntimeError("Search failed")
        response = client.post(
            "/api/v2/memories/search",
            json={"query": "fail"},
        )
        assert response.status_code == 500
        assert "Failed to search memories" in response.json()["detail"]


# =============================================================================
# Test: POST /api/v2/memories/query
# =============================================================================


class TestQueryMemories:
    def test_query_success(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories/query",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert "filters" in data

    def test_query_with_temporal_filters(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories/query",
            json={
                "as_of_event": "2025-01-01T00:00:00",
                "as_of_system": "2025-06-01T00:00:00",
                "include_invalid": True,
            },
        )
        assert response.status_code == 200

    def test_query_with_offset(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories/query",
            json={"offset": 10, "limit": 5},
        )
        assert response.status_code == 200


# =============================================================================
# Test: POST /api/v2/memories/batch
# =============================================================================


class TestBatchStoreMemories:
    def test_batch_success(self, client, mock_memory_api):
        response = client.post(
            "/api/v2/memories/batch",
            json={
                "memories": [
                    {"content": "mem 1", "scope": "user"},
                    {"content": "mem 2", "scope": "user"},
                ]
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["stored"] == 2
        assert data["failed"] == 0

    def test_batch_partial_failure(self, client, mock_memory_api):
        call_count = 0

        def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Store failed")
            return f"mem-{call_count}"

        mock_memory_api.store.side_effect = _side_effect
        response = client.post(
            "/api/v2/memories/batch",
            json={
                "memories": [
                    {"content": "ok", "scope": "user"},
                    {"content": "fail", "scope": "user"},
                    {"content": "ok2", "scope": "user"},
                ]
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["stored"] == 2
        assert data["failed"] == 1


# =============================================================================
# Test: GET /api/v2/memories/{id}/history
# =============================================================================


class TestGetMemoryHistory:
    def test_history_success(self, client, mock_memory_api):
        response = client.get("/api/v2/memories/mem-123/history")
        assert response.status_code == 200
        data = response.json()
        assert data["memory_id"] == "mem-123"
        assert data["current_version"] == 2

    def test_history_not_found(self, client, mock_memory_api):
        mock_memory_api.resolve_to_current.return_value = None
        response = client.get("/api/v2/memories/nonexistent/history")
        assert response.status_code == 404


# =============================================================================
# Test: GET /api/v2/memories/{id}/versions/{ver}
# =============================================================================


class TestGetMemoryVersion:
    def test_version_success(self, client, mock_memory_api):
        response = client.get("/api/v2/memories/mem-123/versions/1")
        assert response.status_code == 200

    def test_version_not_found(self, client, mock_memory_api):
        mock_memory_api.get_version.return_value = None
        response = client.get("/api/v2/memories/mem-123/versions/99")
        assert response.status_code == 404


# =============================================================================
# Test: POST /api/v2/memories/{id}/rollback
# =============================================================================


class TestRollbackMemory:
    def test_rollback_success(self, client, mock_memory_api):
        response = client.post("/api/v2/memories/mem-123/rollback?version=1")
        assert response.status_code == 200
        data = response.json()
        assert data["rolled_back"] is True
        assert data["rolled_back_to_version"] == 1

    def test_rollback_not_found(self, client, mock_memory_api):
        mock_memory_api.rollback.side_effect = ValueError("Version not found")
        response = client.post("/api/v2/memories/mem-123/rollback?version=99")
        assert response.status_code == 404


# =============================================================================
# Test: GET /api/v2/memories/{id}/diff
# =============================================================================


class TestDiffMemoryVersions:
    def test_diff_metadata_mode(self, client, mock_memory_api):
        response = client.get("/api/v2/memories/mem-123/diff?v1=1&v2=2&mode=metadata")
        assert response.status_code == 200

    def test_diff_content_mode(self, client, mock_memory_api):
        mock_memory_api.diff_versions.return_value = "--- v1\n+++ v2\n-old\n+new"
        response = client.get("/api/v2/memories/mem-123/diff?v1=1&v2=2&mode=content")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "content"

    def test_diff_not_found(self, client, mock_memory_api):
        mock_memory_api.diff_versions.side_effect = ValueError("Version not found")
        response = client.get("/api/v2/memories/mem-123/diff?v1=1&v2=99")
        assert response.status_code == 404


# =============================================================================
# Test: GET /api/v2/memories/{id}/lineage
# =============================================================================


class TestGetMemoryLineage:
    def test_lineage_success(self, client, mock_memory_api):
        response = client.get("/api/v2/memories/mem-123/lineage")
        assert response.status_code == 200
        data = response.json()
        assert data["chain_length"] == 2

    def test_lineage_not_found(self, client, mock_memory_api):
        mock_memory_api.resolve_to_current.return_value = None
        response = client.get("/api/v2/memories/nonexistent/lineage")
        assert response.status_code == 404


# =============================================================================
# Test: GET /api/v2/memories/stats
# =============================================================================


class TestGetMemoryPagingStats:
    def test_stats_with_paging(self, client, mock_memory_api):
        mock_memory_api.get_paging_stats = MagicMock(
            return_value={"paging_enabled": True, "hot_tier": 100}
        )
        response = client.get("/api/v2/memories/stats")
        assert response.status_code == 200
        assert response.json()["paging_enabled"] is True

    def test_stats_without_paging(self, client, mock_memory_api):
        # Remove get_paging_stats to simulate no paging support
        if hasattr(mock_memory_api, "get_paging_stats"):
            delattr(mock_memory_api, "get_paging_stats")
        response = client.get("/api/v2/memories/stats")
        assert response.status_code == 200
        assert response.json()["paging_enabled"] is False


# =============================================================================
# Test: Auth enforcement (parametrized)
# =============================================================================


class TestAuthEnforcement:
    """Verify all endpoints reject unauthenticated requests."""

    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("POST", "/api/v2/memories", {"content": "test", "scope": "user"}),
            ("GET", "/api/v2/memories/mem-123", None),
            ("PUT", "/api/v2/memories/mem-123", {"content": "updated"}),
            ("DELETE", "/api/v2/memories/mem-123", None),
            ("POST", "/api/v2/memories/mem-123/invalidate", None),
            ("POST", "/api/v2/memories/mem-123/revalidate", None),
            ("POST", "/api/v2/memories/search", {"query": "test"}),
            ("POST", "/api/v2/memories/query", {}),
            ("POST", "/api/v2/memories/batch", {"memories": []}),
            ("GET", "/api/v2/memories/mem-123/history", None),
            ("GET", "/api/v2/memories/mem-123/versions/1", None),
            ("POST", "/api/v2/memories/mem-123/rollback?version=1", None),
            ("GET", "/api/v2/memories/mem-123/diff?v1=1&v2=2", None),
            ("GET", "/api/v2/memories/mem-123/lineage", None),
            ("GET", "/api/v2/memories/stats", None),
        ],
    )
    def test_rejects_unauthenticated(self, client_no_auth, method, path, json_body):
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        response = getattr(client_no_auth, method.lower())(path, **kwargs)
        assert response.status_code == 401
