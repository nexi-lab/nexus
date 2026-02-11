"""Tests for Trajectory REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers all 5 trajectory endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_nexus_fs,
    get_trajectory_manager,
)
from nexus.server.api.v2.routers.trajectories import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(mock_nexus_fs, mock_trajectory_manager, mock_auth_result):
    """Create test FastAPI app with trajectories router."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_traj_manager():
        return mock_trajectory_manager

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_trajectory_manager] = _mock_traj_manager
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
# Test: POST /api/v2/trajectories (start)
# =============================================================================


class TestStartTrajectory:
    def test_start_success(self, client, mock_trajectory_manager):
        response = client.post(
            "/api/v2/trajectories",
            json={"task_description": "Implement feature X"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["trajectory_id"] == "traj-123"
        assert data["status"] == "in_progress"

    def test_start_with_all_fields(self, client, mock_trajectory_manager):
        response = client.post(
            "/api/v2/trajectories",
            json={
                "task_description": "Full test",
                "task_type": "coding",
                "parent_trajectory_id": "traj-parent",
                "metadata": {"env": "test"},
                "path": "/src/test.py",
            },
        )
        assert response.status_code == 201

    def test_start_error(self, client, mock_trajectory_manager):
        mock_trajectory_manager.start_trajectory.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/trajectories",
            json={"task_description": "fail"},
        )
        assert response.status_code == 500
        assert "Failed to start trajectory" in response.json()["detail"]


# =============================================================================
# Test: POST /api/v2/trajectories/{id}/steps
# =============================================================================


class TestLogStep:
    def test_log_step_success(self, client, mock_trajectory_manager):
        response = client.post(
            "/api/v2/trajectories/traj-123/steps",
            json={"step_type": "action", "description": "Ran tests"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "logged"

    def test_log_step_not_found(self, client, mock_trajectory_manager):
        mock_trajectory_manager.log_step.side_effect = ValueError("Trajectory not found")
        response = client.post(
            "/api/v2/trajectories/nonexistent/steps",
            json={"step_type": "action", "description": "test"},
        )
        assert response.status_code == 404

    def test_log_step_error(self, client, mock_trajectory_manager):
        mock_trajectory_manager.log_step.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/trajectories/traj-123/steps",
            json={"step_type": "action", "description": "fail"},
        )
        assert response.status_code == 500


# =============================================================================
# Test: POST /api/v2/trajectories/{id}/complete
# =============================================================================


class TestCompleteTrajectory:
    def test_complete_success(self, client, mock_trajectory_manager):
        response = client.post(
            "/api/v2/trajectories/traj-123/complete",
            json={"status": "success"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["completed"] is True
        assert data["status"] == "success"

    def test_complete_with_score(self, client, mock_trajectory_manager):
        response = client.post(
            "/api/v2/trajectories/traj-123/complete",
            json={
                "status": "failure",
                "success_score": 0.3,
                "error_message": "Test failed",
                "metrics": {"duration": 120},
            },
        )
        assert response.status_code == 200

    def test_complete_not_found(self, client, mock_trajectory_manager):
        mock_trajectory_manager.complete_trajectory.side_effect = ValueError("Not found")
        response = client.post(
            "/api/v2/trajectories/nonexistent/complete",
            json={"status": "success"},
        )
        assert response.status_code == 404


# =============================================================================
# Test: GET /api/v2/trajectories (query)
# =============================================================================


class TestQueryTrajectories:
    def test_query_success(self, client, mock_trajectory_manager):
        response = client.get("/api/v2/trajectories")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert "filters" in data

    def test_query_with_filters(self, client, mock_trajectory_manager):
        response = client.get(
            "/api/v2/trajectories?agent_id=ag-1&task_type=coding&status=in_progress&limit=10&offset=5"
        )
        assert response.status_code == 200

    def test_query_error(self, client, mock_trajectory_manager):
        mock_trajectory_manager.query_trajectories.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/trajectories")
        assert response.status_code == 500


# =============================================================================
# Test: GET /api/v2/trajectories/{id}
# =============================================================================


class TestGetTrajectory:
    def test_get_success(self, client, mock_trajectory_manager):
        response = client.get("/api/v2/trajectories/traj-123")
        assert response.status_code == 200
        assert response.json()["trajectory"]["trajectory_id"] == "traj-123"

    def test_get_not_found(self, client, mock_trajectory_manager):
        mock_trajectory_manager.get_trajectory.return_value = None
        response = client.get("/api/v2/trajectories/nonexistent")
        assert response.status_code == 404

    def test_get_error(self, client, mock_trajectory_manager):
        mock_trajectory_manager.get_trajectory.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/trajectories/traj-123")
        assert response.status_code == 500


# =============================================================================
# Test: Auth enforcement (parametrized)
# =============================================================================


class TestAuthEnforcement:
    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("POST", "/api/v2/trajectories", {"task_description": "test"}),
            ("POST", "/api/v2/trajectories/t-1/steps", {"step_type": "action", "description": "x"}),
            ("POST", "/api/v2/trajectories/t-1/complete", {"status": "success"}),
            ("GET", "/api/v2/trajectories", None),
            ("GET", "/api/v2/trajectories/t-1", None),
        ],
    )
    def test_rejects_unauthenticated(self, client_no_auth, method, path, json_body):
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        response = getattr(client_no_auth, method.lower())(path, **kwargs)
        assert response.status_code == 401
