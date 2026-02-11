"""Tests for Curate REST API router.

Tests for issue #1193: Expose Comprehensive Memory & ACE REST APIs.
Covers the 2 curate endpoints.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_curator,
    get_nexus_fs,
)
from nexus.server.api.v2.routers.curate import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app(mock_nexus_fs, mock_curator, mock_auth_result):
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return mock_auth_result

    async def _mock_curator():
        return mock_curator

    async def _mock_nexus_fs():
        return mock_nexus_fs

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_curator] = _mock_curator
    app.dependency_overrides[get_nexus_fs] = _mock_nexus_fs

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def app_no_auth(mock_nexus_fs):
    """App where auth rejects — no curator override so nested auth triggers."""
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
# Test: POST /api/v2/curate
# =============================================================================


class TestCurate:
    def test_curate_success(self, client, mock_curator):
        response = client.post(
            "/api/v2/curate",
            json={
                "playbook_id": "pb-123",
                "reflection_memory_ids": ["mem-1", "mem-2"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["playbook_id"] == "pb-123"
        assert data["strategies_added"] == 2
        assert data["strategies_merged"] == 1

    def test_curate_with_threshold(self, client, mock_curator):
        response = client.post(
            "/api/v2/curate",
            json={
                "playbook_id": "pb-123",
                "reflection_memory_ids": ["mem-1"],
                "merge_threshold": 0.9,
            },
        )
        assert response.status_code == 200

    def test_curate_not_found(self, client, mock_curator):
        mock_curator.curate_playbook.side_effect = ValueError("Playbook not found")
        response = client.post(
            "/api/v2/curate",
            json={
                "playbook_id": "nonexistent",
                "reflection_memory_ids": ["mem-1"],
            },
        )
        assert response.status_code == 404

    def test_curate_error(self, client, mock_curator):
        mock_curator.curate_playbook.side_effect = RuntimeError("DB error")
        response = client.post(
            "/api/v2/curate",
            json={
                "playbook_id": "pb-123",
                "reflection_memory_ids": ["mem-1"],
            },
        )
        assert response.status_code == 500
        assert "Failed to curate memories" in response.json()["detail"]


# =============================================================================
# Test: POST /api/v2/curate/bulk
# =============================================================================


class TestCurateBulk:
    def test_bulk_success(self, client, mock_curator):
        response = client.post(
            "/api/v2/curate/bulk",
            json={
                "playbook_id": "pb-123",
                "trajectory_ids": ["traj-1", "traj-2"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2
        assert data["failed"] == 0

    def test_bulk_partial_failure(self, client, mock_curator):
        call_count = 0

        def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Trajectory failed")
            return {"strategies_added": 1, "strategies_merged": 0, "strategies_total": 1}

        mock_curator.curate_from_trajectory.side_effect = _side_effect
        response = client.post(
            "/api/v2/curate/bulk",
            json={
                "playbook_id": "pb-123",
                "trajectory_ids": ["traj-1", "traj-2", "traj-3"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2
        assert data["failed"] == 1

    def test_bulk_error(self, client, mock_curator):
        mock_curator.curate_from_trajectory.side_effect = RuntimeError("Total failure")
        response = client.post(
            "/api/v2/curate/bulk",
            json={
                "playbook_id": "pb-123",
                "trajectory_ids": ["traj-1"],
            },
        )
        # Individual trajectory failures are caught; only overall error causes 500
        assert response.status_code == 200
        data = response.json()
        assert data["failed"] == 1


# =============================================================================
# Test: Auth enforcement (parametrized)
# =============================================================================


class TestAuthEnforcement:
    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            (
                "POST",
                "/api/v2/curate",
                {"playbook_id": "pb-1", "reflection_memory_ids": ["m-1"]},
            ),
            (
                "POST",
                "/api/v2/curate/bulk",
                {"playbook_id": "pb-1", "trajectory_ids": ["t-1"]},
            ),
        ],
    )
    def test_rejects_unauthenticated(self, client_no_auth, method, path, json_body):
        response = getattr(client_no_auth, method.lower())(path, json=json_body)
        assert response.status_code == 401
