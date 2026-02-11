"""Tests for Feedback REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers all 5 feedback endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_feedback_manager,
    get_nexus_fs,
)
from nexus.server.api.v2.routers.feedback import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(mock_nexus_fs, mock_feedback_manager, mock_auth_result):
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_feedback():
        return mock_feedback_manager

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_feedback_manager] = _mock_feedback
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def app_no_auth(mock_nexus_fs, mock_feedback_manager):
    """App where auth rejects all requests.

    feedback.py uses ``Depends(_get_require_auth())`` directly on each endpoint,
    so overriding the auth dep is sufficient. We still override get_feedback_manager
    and get_nexus_fs so that FastAPI doesn't try to resolve NexusFS from real state.
    """
    app = FastAPI()
    app.include_router(router)

    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _mock_feedback():
        return mock_feedback_manager

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _reject_auth
    app.dependency_overrides[get_feedback_manager] = _mock_feedback
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client_no_auth(app_no_auth):
    return TestClient(app_no_auth, raise_server_exceptions=False)


# =============================================================================
# Test: POST /api/v2/feedback (add)
# =============================================================================


class TestAddFeedback:
    def test_add_success(self, client, mock_feedback_manager):
        response = client.post(
            "/api/v2/feedback",
            json={
                "trajectory_id": "traj-123",
                "feedback_type": "human",
                "score": 0.9,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["feedback_id"] == "fb-123"
        assert data["status"] == "created"

    def test_add_with_all_fields(self, client, mock_feedback_manager):
        response = client.post(
            "/api/v2/feedback",
            json={
                "trajectory_id": "traj-123",
                "feedback_type": "monitoring",
                "score": 0.5,
                "source": "prometheus",
                "message": "High latency",
                "metrics": {"latency_ms": 500},
            },
        )
        assert response.status_code == 201

    def test_add_not_found(self, client, mock_feedback_manager):
        mock_feedback_manager.add_feedback.side_effect = ValueError("Trajectory not found")
        response = client.post(
            "/api/v2/feedback",
            json={"trajectory_id": "nonexistent", "feedback_type": "human"},
        )
        assert response.status_code == 404

    def test_add_error(self, client, mock_feedback_manager):
        mock_feedback_manager.add_feedback.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/feedback",
            json={"trajectory_id": "traj-123", "feedback_type": "human"},
        )
        assert response.status_code == 500
        assert "Failed to add feedback" in response.json()["detail"]


# =============================================================================
# Test: GET /api/v2/feedback/queue
# =============================================================================


class TestGetRelearningQueue:
    def test_queue_success(self, client, mock_feedback_manager):
        response = client.get("/api/v2/feedback/queue")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    def test_queue_with_limit(self, client, mock_feedback_manager):
        response = client.get("/api/v2/feedback/queue?limit=5&offset=0")
        assert response.status_code == 200

    def test_queue_error(self, client, mock_feedback_manager):
        mock_feedback_manager.get_relearning_queue.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/feedback/queue")
        assert response.status_code == 500


# =============================================================================
# Test: POST /api/v2/feedback/score
# =============================================================================


class TestCalculateScore:
    def test_score_success(self, client, mock_feedback_manager):
        response = client.post(
            "/api/v2/feedback/score",
            json={"trajectory_id": "traj-123", "strategy": "latest"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["effective_score"] == 0.85
        assert data["strategy"] == "latest"

    def test_score_not_found(self, client, mock_feedback_manager):
        mock_feedback_manager.get_effective_score.side_effect = ValueError("Not found")
        response = client.post(
            "/api/v2/feedback/score",
            json={"trajectory_id": "nonexistent", "strategy": "latest"},
        )
        assert response.status_code == 404


# =============================================================================
# Test: POST /api/v2/feedback/relearn
# =============================================================================


class TestMarkForRelearning:
    def test_relearn_success(self, client, mock_feedback_manager):
        response = client.post(
            "/api/v2/feedback/relearn",
            json={
                "trajectory_id": "traj-123",
                "reason": "Low score",
                "priority": 8,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["marked"] is True
        assert data["priority"] == 8

    def test_relearn_not_found(self, client, mock_feedback_manager):
        mock_feedback_manager.mark_for_relearning.side_effect = ValueError("Not found")
        response = client.post(
            "/api/v2/feedback/relearn",
            json={"trajectory_id": "nonexistent", "reason": "test", "priority": 5},
        )
        assert response.status_code == 404


# =============================================================================
# Test: GET /api/v2/feedback/{trajectory_id}
# =============================================================================


class TestGetTrajectoryFeedback:
    def test_get_success(self, client, mock_feedback_manager):
        response = client.get("/api/v2/feedback/traj-123")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["trajectory_id"] == "traj-123"

    def test_get_error(self, client, mock_feedback_manager):
        mock_feedback_manager.get_trajectory_feedback.side_effect = RuntimeError("DB error")
        response = client.get("/api/v2/feedback/traj-123")
        assert response.status_code == 500


# =============================================================================
# Test: Auth enforcement (parametrized)
# =============================================================================


class TestAuthEnforcement:
    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("POST", "/api/v2/feedback", {"trajectory_id": "t-1", "feedback_type": "human"}),
            ("GET", "/api/v2/feedback/queue", None),
            ("POST", "/api/v2/feedback/score", {"trajectory_id": "t-1", "strategy": "latest"}),
            ("POST", "/api/v2/feedback/relearn", {"trajectory_id": "t-1", "reason": "x"}),
            ("GET", "/api/v2/feedback/traj-123", None),
        ],
    )
    def test_rejects_unauthenticated(self, client_no_auth, method, path, json_body):
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        response = getattr(client_no_auth, method.lower())(path, **kwargs)
        assert response.status_code == 401
