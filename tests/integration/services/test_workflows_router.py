"""Unit tests for the workflows API router.

Issue #1522: Tests for all 8 workflow endpoints using FastAPI TestClient
with a mock workflow engine.
"""

import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.workflows.engine import WorkflowEngine
from nexus.bricks.workflows.types import (
    TriggerType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowTrigger,
)
from nexus.server.api.v2.routers.workflows import (
    _get_require_auth,
    _get_workflow_engine,
    router,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(router)


def _make_mock_engine() -> Mock:
    """Create a mock workflow engine with default return values."""
    engine = Mock(spec=WorkflowEngine)
    engine.workflows = {}
    engine.enabled_workflows = {}
    engine.workflow_ids = {}
    engine.workflow_store = None
    engine.list_workflows.return_value = []
    engine.load_workflow.return_value = True
    engine.unload_workflow.return_value = True
    return engine


_mock_engine = _make_mock_engine()


def _override_engine() -> Mock:
    return _mock_engine


def _override_auth() -> dict:
    """Override auth dependency — return a dummy auth result."""
    return {"subject_id": "test-user", "zone_id": "root", "is_admin": True}


# Override dependencies
_test_app.dependency_overrides[_get_workflow_engine] = _override_engine
_test_app.dependency_overrides[_get_require_auth()] = _override_auth

client = TestClient(_test_app)


@pytest.fixture(autouse=True)
def reset_engine():
    """Reset mock engine before each test."""
    global _mock_engine
    _mock_engine = _make_mock_engine()
    _test_app.dependency_overrides[_get_workflow_engine] = lambda: _mock_engine
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListWorkflows:
    """Test GET /api/v2/workflows."""

    def test_list_empty(self):
        """Test listing when no workflows loaded."""
        _mock_engine.list_workflows.return_value = []
        resp = client.get("/api/v2/workflows")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_workflows(self):
        """Test listing loaded workflows."""
        _mock_engine.list_workflows.return_value = [
            {
                "name": "wf1",
                "version": "1.0",
                "description": "First",
                "enabled": True,
                "triggers": 1,
                "actions": 2,
            },
            {
                "name": "wf2",
                "version": "2.0",
                "description": None,
                "enabled": False,
                "triggers": 0,
                "actions": 1,
            },
        ]

        resp = client.get("/api/v2/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "wf1"
        assert data[0]["enabled"] is True
        assert data[1]["name"] == "wf2"
        assert data[1]["enabled"] is False


class TestCreateWorkflow:
    """Test POST /api/v2/workflows."""

    @patch("nexus.bricks.workflows.loader.WorkflowLoader")
    def test_create_workflow(self, mock_loader):
        """Test creating a workflow."""
        definition = WorkflowDefinition(
            name="test-wf",
            version="1.0",
            description="Test workflow",
            actions=[WorkflowAction(name="a1", type="python", config={"code": "pass"})],
        )
        mock_loader.load_from_dict.return_value = definition

        resp = client.post(
            "/api/v2/workflows",
            json={
                "name": "test-wf",
                "version": "1.0",
                "description": "Test workflow",
                "actions": [{"name": "a1", "type": "python", "code": "pass"}],
                "enabled": True,
            },
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-wf"
        assert data["version"] == "1.0"
        assert data["enabled"] is True
        _mock_engine.load_workflow.assert_called_once()

    @patch("nexus.bricks.workflows.loader.WorkflowLoader")
    def test_create_workflow_invalid(self, mock_loader):
        """Test creating a workflow with invalid definition."""
        mock_loader.load_from_dict.side_effect = ValueError("bad definition")

        resp = client.post(
            "/api/v2/workflows",
            json={
                "name": "bad-wf",
                "actions": [{"name": "a1", "type": "python"}],
            },
        )

        assert resp.status_code == 400
        assert "Invalid workflow definition" in resp.json()["detail"]

    @patch("nexus.bricks.workflows.loader.WorkflowLoader")
    def test_create_workflow_load_failure(self, mock_loader):
        """Test creating a workflow when engine load fails."""
        definition = WorkflowDefinition(
            name="fail-wf",
            version="1.0",
            actions=[WorkflowAction(name="a1", type="python")],
        )
        mock_loader.load_from_dict.return_value = definition
        _mock_engine.load_workflow.return_value = False

        resp = client.post(
            "/api/v2/workflows",
            json={
                "name": "fail-wf",
                "actions": [{"name": "a1", "type": "python"}],
            },
        )

        assert resp.status_code == 400
        assert "Failed to load workflow" in resp.json()["detail"]


class TestGetWorkflow:
    """Test GET /api/v2/workflows/{name}."""

    def test_get_existing(self):
        """Test getting an existing workflow."""
        definition = WorkflowDefinition(
            name="my-wf",
            version="1.0",
            description="My workflow",
            triggers=[WorkflowTrigger(type=TriggerType.FILE_WRITE, config={"pattern": "*.md"})],
            actions=[WorkflowAction(name="a1", type="python", config={"code": "pass"})],
            variables={"env": "prod"},
        )
        _mock_engine.workflows = {"my-wf": definition}
        _mock_engine.enabled_workflows = {"my-wf": True}

        resp = client.get("/api/v2/workflows/my-wf")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-wf"
        assert data["version"] == "1.0"
        assert data["enabled"] is True
        assert len(data["triggers"]) == 1
        assert len(data["actions"]) == 1
        assert data["variables"] == {"env": "prod"}

    def test_get_nonexistent(self):
        """Test getting a non-existent workflow."""
        _mock_engine.workflows = {}
        resp = client.get("/api/v2/workflows/nonexistent")
        assert resp.status_code == 404


class TestDeleteWorkflow:
    """Test DELETE /api/v2/workflows/{name}."""

    def test_delete_existing(self):
        """Test deleting an existing workflow."""
        _mock_engine.unload_workflow.return_value = True
        resp = client.delete("/api/v2/workflows/my-wf")
        assert resp.status_code == 204
        _mock_engine.unload_workflow.assert_called_once_with("my-wf")

    def test_delete_nonexistent(self):
        """Test deleting a non-existent workflow."""
        _mock_engine.unload_workflow.return_value = False
        resp = client.delete("/api/v2/workflows/nonexistent")
        assert resp.status_code == 404


class TestEnableWorkflow:
    """Test POST /api/v2/workflows/{name}/enable."""

    def test_enable_existing(self):
        """Test enabling an existing workflow."""
        _mock_engine.workflows = {"my-wf": Mock()}
        resp = client.post("/api/v2/workflows/my-wf/enable")
        assert resp.status_code == 204
        _mock_engine.enable_workflow.assert_called_once_with("my-wf")

    def test_enable_nonexistent(self):
        """Test enabling a non-existent workflow."""
        _mock_engine.workflows = {}
        resp = client.post("/api/v2/workflows/nonexistent/enable")
        assert resp.status_code == 404


class TestDisableWorkflow:
    """Test POST /api/v2/workflows/{name}/disable."""

    def test_disable_existing(self):
        """Test disabling an existing workflow."""
        _mock_engine.workflows = {"my-wf": Mock()}
        resp = client.post("/api/v2/workflows/my-wf/disable")
        assert resp.status_code == 204
        _mock_engine.disable_workflow.assert_called_once_with("my-wf")

    def test_disable_nonexistent(self):
        """Test disabling a non-existent workflow."""
        _mock_engine.workflows = {}
        resp = client.post("/api/v2/workflows/nonexistent/disable")
        assert resp.status_code == 404


class TestExecuteWorkflow:
    """Test POST /api/v2/workflows/{name}/execute."""

    def test_execute_success(self):
        """Test successful workflow execution."""
        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            workflow_name="my-wf",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
            actions_completed=2,
            actions_total=2,
        )
        _mock_engine.trigger_workflow = AsyncMock(return_value=execution)

        resp = client.post(
            "/api/v2/workflows/my-wf/execute",
            json={"file_path": "/test/file.txt", "context": {"env": "test"}},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "succeeded"
        assert data["actions_completed"] == 2
        assert data["actions_total"] == 2

    def test_execute_not_found(self):
        """Test executing non-existent or disabled workflow."""
        _mock_engine.trigger_workflow = AsyncMock(return_value=None)

        resp = client.post("/api/v2/workflows/nonexistent/execute")
        assert resp.status_code == 404

    def test_execute_no_body(self):
        """Test executing with no request body."""
        execution = WorkflowExecution(
            execution_id=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            workflow_name="my-wf",
            status=WorkflowStatus.SUCCEEDED,
            trigger_type=TriggerType.MANUAL,
            trigger_context={},
        )
        _mock_engine.trigger_workflow = AsyncMock(return_value=execution)

        resp = client.post("/api/v2/workflows/my-wf/execute")
        assert resp.status_code == 200


class TestGetExecutions:
    """Test GET /api/v2/workflows/{name}/executions."""

    def test_get_executions_no_store(self):
        """Test getting executions when no store is available."""
        _mock_engine.workflows = {"my-wf": Mock()}
        _mock_engine.workflow_store = None

        resp = client.get("/api/v2/workflows/my-wf/executions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_executions_not_found(self):
        """Test getting executions for non-existent workflow."""
        _mock_engine.workflows = {}

        resp = client.get("/api/v2/workflows/nonexistent/executions")
        assert resp.status_code == 404

    def test_get_executions_with_store(self):
        """Test getting executions with mock store."""
        _mock_engine.workflows = {"my-wf": Mock()}
        mock_store = Mock()
        mock_store.get_executions = AsyncMock(
            return_value=[
                {
                    "execution_id": str(uuid.uuid4()),
                    "workflow_id": str(uuid.uuid4()),
                    "trigger_type": "manual",
                    "status": "succeeded",
                    "started_at": "2025-01-01T00:00:00",
                    "completed_at": "2025-01-01T00:00:01",
                    "actions_completed": 1,
                    "actions_total": 1,
                    "error_message": None,
                }
            ]
        )
        _mock_engine.workflow_store = mock_store

        resp = client.get("/api/v2/workflows/my-wf/executions?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["status"] == "succeeded"
        mock_store.get_executions.assert_called_once_with(name="my-wf", limit=5)
