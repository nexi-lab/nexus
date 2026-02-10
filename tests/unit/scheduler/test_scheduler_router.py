"""Tests for Scheduler REST API router.

Test categories:
1. Submit task endpoint
2. Get task status endpoint
3. Cancel task endpoint
4. Validation errors
5. Exception handler mapping

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.scheduler.constants import TASK_STATUS_QUEUED, PriorityTier
from nexus.scheduler.models import ScheduledTask
from nexus.server.api.v2.routers.scheduler import (
    _get_require_auth,
    get_scheduler_service,
    router,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_scheduler_service():
    """Mock SchedulerService."""
    now = datetime.now(UTC)
    service = AsyncMock()
    service.submit_task = AsyncMock(
        return_value=ScheduledTask(
            id="task-uuid-123",
            agent_id="test-agent",
            executor_id="agent-b",
            task_type="process",
            payload={"key": "value"},
            priority_tier=PriorityTier.NORMAL,
            effective_tier=2,
            enqueued_at=now,
            status=TASK_STATUS_QUEUED,
        )
    )
    service.get_status = AsyncMock(
        return_value=ScheduledTask(
            id="task-uuid-123",
            agent_id="test-agent",
            executor_id="agent-b",
            task_type="process",
            payload={"key": "value"},
            priority_tier=PriorityTier.NORMAL,
            effective_tier=2,
            enqueued_at=now,
            status=TASK_STATUS_QUEUED,
        )
    )
    service.cancel_task = AsyncMock(return_value=True)
    return service


@pytest.fixture
def mock_auth_result():
    """Mock auth result for authenticated user."""
    return {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "test-agent",
        "zone_id": "test-tenant",
        "is_admin": False,
    }


@pytest.fixture
def app(mock_scheduler_service, mock_auth_result):
    """Create test FastAPI app with scheduler router."""
    app = FastAPI()
    app.include_router(router)

    # Override DI
    app.dependency_overrides[get_scheduler_service] = lambda: mock_scheduler_service
    app.dependency_overrides[_get_require_auth()] = lambda: mock_auth_result

    return app


@pytest.fixture
def client(app):
    """Test client."""
    return TestClient(app)


# =============================================================================
# 1. Submit Task Endpoint
# =============================================================================


class TestSubmitEndpoint:
    """Test POST /api/v2/scheduler/submit."""

    def test_submit_basic(self, client, mock_scheduler_service):
        """Should submit a task and return task details."""
        payload = {
            "executor": "agent-b",
            "task_type": "process",
            "payload": {"key": "value"},
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "task-uuid-123"
        assert data["status"] == "queued"
        assert data["priority_tier"] == "normal"
        assert data["effective_tier"] == 2

    def test_submit_with_priority(self, client):
        """Should accept priority parameter."""
        payload = {
            "executor": "agent-b",
            "task_type": "urgent",
            "priority": "high",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 201

    def test_submit_with_boost(self, client):
        """Should accept boost parameter."""
        payload = {
            "executor": "agent-b",
            "task_type": "process",
            "boost": "0.02",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 201

    def test_submit_with_deadline(self, client):
        """Should accept deadline parameter."""
        deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        payload = {
            "executor": "agent-b",
            "task_type": "timed",
            "deadline": deadline,
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 201

    def test_submit_with_idempotency_key(self, client):
        """Should accept idempotency_key parameter."""
        payload = {
            "executor": "agent-b",
            "task_type": "process",
            "idempotency_key": "unique-123",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 201

    def test_submit_missing_executor(self, client):
        """Should reject missing executor."""
        payload = {"task_type": "process"}

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 422

    def test_submit_missing_task_type(self, client):
        """Should reject missing task_type."""
        payload = {"executor": "agent-b"}

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 422

    def test_submit_invalid_priority(self, client):
        """Should reject invalid priority string."""
        payload = {
            "executor": "agent-b",
            "task_type": "process",
            "priority": "super_urgent",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 422


# =============================================================================
# 2. Get Task Status Endpoint
# =============================================================================


class TestGetStatusEndpoint:
    """Test GET /api/v2/scheduler/task/{id}."""

    def test_get_existing_task(self, client):
        """Should return task details."""
        response = client.get("/api/v2/scheduler/task/task-uuid-123")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "task-uuid-123"
        assert data["status"] == "queued"

    def test_get_nonexistent_task(self, client, mock_scheduler_service):
        """Should return 404 for non-existent task."""
        mock_scheduler_service.get_status = AsyncMock(return_value=None)

        response = client.get("/api/v2/scheduler/task/nonexistent")
        assert response.status_code == 404


# =============================================================================
# 3. Cancel Task Endpoint
# =============================================================================


class TestCancelEndpoint:
    """Test POST /api/v2/scheduler/task/{id}/cancel."""

    def test_cancel_success(self, client):
        """Should cancel a queued task."""
        response = client.post("/api/v2/scheduler/task/task-uuid-123/cancel")

        assert response.status_code == 200
        data = response.json()
        assert data["cancelled"] is True

    def test_cancel_not_cancellable(self, client, mock_scheduler_service):
        """Should return cancelled=false for non-cancellable task."""
        mock_scheduler_service.cancel_task = AsyncMock(return_value=False)

        response = client.post("/api/v2/scheduler/task/task-uuid-123/cancel")

        assert response.status_code == 200
        data = response.json()
        assert data["cancelled"] is False
