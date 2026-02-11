"""Tests for Playbook REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers all 6 playbook endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_nexus_fs,
    get_playbook_manager,
)
from nexus.server.api.v2.routers.playbooks import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(mock_nexus_fs, mock_playbook_manager, mock_auth_result):
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_playbook():
        return mock_playbook_manager

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_playbook_manager] = _mock_playbook
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def app_no_auth(mock_nexus_fs):
    """App where auth rejects — no manager override so nested auth triggers."""
    app = FastAPI()
    app.include_router(router)

    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _reject_auth
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client_no_auth(app_no_auth):
    return TestClient(app_no_auth, raise_server_exceptions=False)


# =============================================================================
# Test: GET /api/v2/playbooks (list)
# =============================================================================


class TestListPlaybooks:
    def test_list_success(self, client, mock_playbook_manager):
        response = client.get("/api/v2/playbooks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    def test_list_with_filters(self, client, mock_playbook_manager):
        response = client.get(
            "/api/v2/playbooks?scope=agent&name_pattern=%25test%25&limit=10&offset=5"
        )
        assert response.status_code == 200

    def test_list_error(self, client, mock_playbook_manager):
        mock_playbook_manager.query_playbooks.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/playbooks")
        assert response.status_code == 500
        assert "Failed to list playbooks" in response.json()["detail"]


# =============================================================================
# Test: POST /api/v2/playbooks (create)
# =============================================================================


class TestCreatePlaybook:
    def test_create_success(self, client, mock_playbook_manager):
        response = client.post(
            "/api/v2/playbooks",
            json={"name": "My Playbook"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["playbook_id"] == "pb-123"
        assert data["status"] == "created"

    def test_create_with_all_fields(self, client, mock_playbook_manager):
        response = client.post(
            "/api/v2/playbooks",
            json={
                "name": "Full Playbook",
                "description": "A comprehensive playbook",
                "scope": "zone",
                "visibility": "shared",
                "initial_strategies": [{"strategy": "plan first", "confidence": 0.9}],
            },
        )
        assert response.status_code == 201

    def test_create_error(self, client, mock_playbook_manager):
        mock_playbook_manager.create_playbook.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/playbooks",
            json={"name": "Fail"},
        )
        assert response.status_code == 500


# =============================================================================
# Test: GET /api/v2/playbooks/{id}
# =============================================================================


class TestGetPlaybook:
    def test_get_success(self, client, mock_playbook_manager):
        response = client.get("/api/v2/playbooks/pb-123")
        assert response.status_code == 200
        assert response.json()["playbook"]["playbook_id"] == "pb-123"

    def test_get_not_found(self, client, mock_playbook_manager):
        mock_playbook_manager.get_playbook.return_value = None
        response = client.get("/api/v2/playbooks/nonexistent")
        assert response.status_code == 404

    def test_get_error(self, client, mock_playbook_manager):
        mock_playbook_manager.get_playbook.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/playbooks/pb-123")
        assert response.status_code == 500


# =============================================================================
# Test: PUT /api/v2/playbooks/{id}
# =============================================================================


class TestUpdatePlaybook:
    def test_update_success(self, client, mock_playbook_manager):
        response = client.put(
            "/api/v2/playbooks/pb-123",
            json={"strategies": [{"strategy": "updated"}]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "updated"

    def test_update_not_found(self, client, mock_playbook_manager):
        mock_playbook_manager.get_playbook.return_value = None
        response = client.put(
            "/api/v2/playbooks/nonexistent",
            json={"strategies": []},
        )
        assert response.status_code == 404


# =============================================================================
# Test: DELETE /api/v2/playbooks/{id}
# =============================================================================


class TestDeletePlaybook:
    def test_delete_success(self, client, mock_playbook_manager):
        response = client.delete("/api/v2/playbooks/pb-123")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

    def test_delete_not_found(self, client, mock_playbook_manager):
        mock_playbook_manager.delete_playbook.return_value = False
        response = client.delete("/api/v2/playbooks/nonexistent")
        assert response.status_code == 404


# =============================================================================
# Test: POST /api/v2/playbooks/{id}/usage
# =============================================================================


class TestRecordUsage:
    def test_usage_success(self, client, mock_playbook_manager):
        response = client.post(
            "/api/v2/playbooks/pb-123/usage",
            json={"success": True, "improvement_score": 0.8},
        )
        assert response.status_code == 200
        assert response.json()["recorded"] is True

    def test_usage_not_found(self, client, mock_playbook_manager):
        mock_playbook_manager.get_playbook.return_value = None
        response = client.post(
            "/api/v2/playbooks/nonexistent/usage",
            json={"success": True},
        )
        assert response.status_code == 404

    def test_usage_error(self, client, mock_playbook_manager):
        mock_playbook_manager.record_usage.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/playbooks/pb-123/usage",
            json={"success": True},
        )
        assert response.status_code == 500


# =============================================================================
# Test: Auth enforcement (parametrized)
# =============================================================================


class TestAuthEnforcement:
    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("GET", "/api/v2/playbooks", None),
            ("POST", "/api/v2/playbooks", {"name": "test"}),
            ("GET", "/api/v2/playbooks/pb-1", None),
            ("PUT", "/api/v2/playbooks/pb-1", {"strategies": []}),
            ("DELETE", "/api/v2/playbooks/pb-1", None),
            ("POST", "/api/v2/playbooks/pb-1/usage", {"success": True}),
        ],
    )
    def test_rejects_unauthenticated(self, client_no_auth, method, path, json_body):
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        response = getattr(client_no_auth, method.lower())(path, **kwargs)
        assert response.status_code == 401
