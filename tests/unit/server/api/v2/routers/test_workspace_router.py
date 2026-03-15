"""Unit tests for the workspace/memory registry REST API (Issue #2987).

Tests all 10 endpoints using FastAPI TestClient with a mock WorkspaceRegistry.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.workspace.workspace_registry import MemoryConfig, WorkspaceConfig
from nexus.server.api.v2.routers.workspace import (
    memory_router,
    workspace_router,
)

# ---------------------------------------------------------------------------
# App setup — isolated test app with dependency overrides
# ---------------------------------------------------------------------------


def _make_mock_registry() -> MagicMock:
    """Create a mock WorkspaceRegistry with sensible defaults."""
    registry = MagicMock()
    registry._workspaces = {}
    registry._memories = {}
    registry.get_workspace.return_value = None
    registry.get_memory.return_value = None
    registry.list_workspaces.return_value = []
    registry.list_memories.return_value = []
    registry.unregister_workspace.return_value = False
    registry.unregister_memory.return_value = False
    return registry


_mock_registry = _make_mock_registry()

# Auth result for all authenticated requests
_auth_result = {"authenticated": True, "subject_id": "test-user", "zone_id": "root"}


def _build_test_app() -> FastAPI:
    """Build a test FastAPI app with overridden dependencies."""
    from nexus.server.api.v2.dependencies import get_workspace_registry
    from nexus.server.api.v2.routers.workspace import _get_require_auth

    app = FastAPI()
    app.include_router(workspace_router)
    app.include_router(memory_router)

    app.dependency_overrides[get_workspace_registry] = lambda: _mock_registry
    app.dependency_overrides[_get_require_auth()] = lambda: _auth_result

    return app


_test_app = _build_test_app()
client = TestClient(_test_app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset mock between tests."""
    _mock_registry.reset_mock()
    _mock_registry._workspaces = {}
    _mock_registry._memories = {}
    _mock_registry.get_workspace.return_value = None
    _mock_registry.get_memory.return_value = None
    _mock_registry.list_workspaces.return_value = []
    _mock_registry.list_memories.return_value = []
    _mock_registry.unregister_workspace.return_value = False
    _mock_registry.unregister_memory.return_value = False
    _mock_registry.register_workspace.side_effect = None
    _mock_registry.register_memory.side_effect = None
    _mock_registry.update_workspace.side_effect = None
    _mock_registry.update_memory.side_effect = None


def _make_ws_db_model(
    path: str = "/my-workspace",
    name: str | None = "main",
    description: str = "A workspace",
    scope: str = "persistent",
    user_id: str | None = "alice",
    agent_id: str | None = None,
    session_id: str | None = None,
    expires_at: datetime | None = None,
    created_by: str | None = "alice",
    created_at: datetime | None = None,
    extra_metadata: str | None = None,
) -> MagicMock:
    """Create a mock WorkspaceConfigModel."""
    model = MagicMock()
    model.path = path
    model.name = name
    model.description = description
    model.scope = scope
    model.user_id = user_id
    model.agent_id = agent_id
    model.session_id = session_id
    model.expires_at = expires_at
    model.created_by = created_by
    model.created_at = created_at or datetime(2026, 3, 14, 10, 0, 0)
    model.extra_metadata = extra_metadata
    return model


def _make_mem_db_model(
    path: str = "/my-memory",
    name: str | None = "kb",
    description: str = "A memory",
    scope: str = "persistent",
    user_id: str | None = "alice",
    agent_id: str | None = None,
    session_id: str | None = None,
    expires_at: datetime | None = None,
    created_by: str | None = "alice",
    created_at: datetime | None = None,
    extra_metadata: str | None = None,
) -> MagicMock:
    """Create a mock MemoryConfigModel."""
    model = MagicMock()
    model.path = path
    model.name = name
    model.description = description
    model.scope = scope
    model.user_id = user_id
    model.agent_id = agent_id
    model.session_id = session_id
    model.expires_at = expires_at
    model.created_by = created_by
    model.created_at = created_at or datetime(2026, 3, 14, 10, 0, 0)
    model.extra_metadata = extra_metadata
    return model


# ---------------------------------------------------------------------------
# Workspace: GET /api/v2/registry/workspaces
# ---------------------------------------------------------------------------


class TestListWorkspaces:
    """Tests for GET /api/v2/registry/workspaces."""

    @patch("nexus.server.api.v2.routers.workspace._list_workspace_db_models")
    def test_empty_list(self, mock_list: MagicMock) -> None:
        mock_list.return_value = []
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["count"] == 0

    @patch("nexus.server.api.v2.routers.workspace._list_workspace_db_models")
    def test_list_with_items(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            _make_ws_db_model("/ws1", name="WS1"),
            _make_ws_db_model("/ws2", name="WS2", scope="session"),
        ]
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        paths = [item["path"] for item in data["items"]]
        assert "/ws1" in paths
        assert "/ws2" in paths

    @patch("nexus.server.api.v2.routers.workspace._list_workspace_db_models")
    def test_list_includes_v050_fields(self, mock_list: MagicMock) -> None:
        expires = datetime(2026, 3, 15, 10, 0, 0)
        mock_list.return_value = [
            _make_ws_db_model(
                "/ws",
                scope="session",
                session_id="sess-123",
                expires_at=expires,
                agent_id="agent-1",
            ),
        ]
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["scope"] == "session"
        assert item["session_id"] == "sess-123"
        assert item["agent_id"] == "agent-1"
        assert "2026-03-15" in item["expires_at"]


# ---------------------------------------------------------------------------
# Workspace: POST /api/v2/registry/workspaces
# ---------------------------------------------------------------------------


class TestRegisterWorkspace:
    """Tests for POST /api/v2/registry/workspaces."""

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_register_success(self, mock_get_db: MagicMock) -> None:
        config = WorkspaceConfig(path="/new-ws", name="New", created_at=datetime.now())
        _mock_registry.register_workspace.return_value = config
        mock_get_db.return_value = _make_ws_db_model("/new-ws", name="New")

        resp = client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/new-ws", "name": "New"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["path"] == "/new-ws"
        assert data["name"] == "New"
        _mock_registry.register_workspace.assert_called_once()

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_register_with_ttl(self, mock_get_db: MagicMock) -> None:
        config = WorkspaceConfig(path="/ws", created_at=datetime.now())
        _mock_registry.register_workspace.return_value = config
        mock_get_db.return_value = _make_ws_db_model(
            "/ws",
            session_id="s1",
            scope="session",
        )

        resp = client.post(
            "/api/v2/registry/workspaces",
            json={
                "path": "/ws",
                "session_id": "s1",
                "ttl_seconds": 3600,
            },
        )
        assert resp.status_code == 201
        # Verify ttl was converted to timedelta
        call_kwargs = _mock_registry.register_workspace.call_args[1]
        assert call_kwargs["ttl"] == timedelta(seconds=3600)
        assert call_kwargs["session_id"] == "s1"

    def test_register_duplicate_returns_409(self) -> None:
        _mock_registry.register_workspace.side_effect = ValueError("already registered")
        resp = client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/existing"},
        )
        assert resp.status_code == 409
        assert "already registered" in resp.json()["detail"]

    def test_register_permission_denied_returns_403(self) -> None:
        _mock_registry.register_workspace.side_effect = PermissionError("not owned")
        resp = client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/ws"},
        )
        assert resp.status_code == 403

    def test_register_validation_empty_path(self) -> None:
        resp = client.post(
            "/api/v2/registry/workspaces",
            json={"path": ""},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Workspace: GET /api/v2/registry/workspaces/{path:path}
# ---------------------------------------------------------------------------


class TestGetWorkspace:
    """Tests for GET /api/v2/registry/workspaces/{path}."""

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_get_found(self, mock_get_db: MagicMock) -> None:
        _mock_registry.get_workspace.return_value = WorkspaceConfig(path="/ws")
        mock_get_db.return_value = _make_ws_db_model("/ws")

        resp = client.get("/api/v2/registry/workspaces/ws")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/ws"

    def test_get_not_found(self) -> None:
        _mock_registry.get_workspace.return_value = None
        resp = client.get("/api/v2/registry/workspaces/nonexistent")
        assert resp.status_code == 404

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_get_with_slashes_in_path(self, mock_get_db: MagicMock) -> None:
        _mock_registry.get_workspace.return_value = WorkspaceConfig(path="my/nested/workspace")
        mock_get_db.return_value = _make_ws_db_model("my/nested/workspace")

        resp = client.get("/api/v2/registry/workspaces/my/nested/workspace")
        assert resp.status_code == 200
        assert resp.json()["path"] == "my/nested/workspace"


# ---------------------------------------------------------------------------
# Workspace: PATCH /api/v2/registry/workspaces/{path:path}
# ---------------------------------------------------------------------------


class TestUpdateWorkspace:
    """Tests for PATCH /api/v2/registry/workspaces/{path}."""

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_update_success(self, mock_get_db: MagicMock) -> None:
        updated = WorkspaceConfig(path="/ws", name="Updated")
        _mock_registry.update_workspace.return_value = updated
        mock_get_db.return_value = _make_ws_db_model("/ws", name="Updated")

        resp = client.patch(
            "/api/v2/registry/workspaces/ws",
            json={"name": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        _mock_registry.update_workspace.assert_called_once_with(
            path="ws",
            name="Updated",
            description=None,
            metadata=None,
        )

    def test_update_not_found(self) -> None:
        _mock_registry.update_workspace.side_effect = ValueError("not found")
        resp = client.patch(
            "/api/v2/registry/workspaces/ws",
            json={"name": "X"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Workspace: DELETE /api/v2/registry/workspaces/{path:path}
# ---------------------------------------------------------------------------


class TestUnregisterWorkspace:
    """Tests for DELETE /api/v2/registry/workspaces/{path}."""

    def test_delete_success(self) -> None:
        _mock_registry.unregister_workspace.return_value = True
        resp = client.delete("/api/v2/registry/workspaces/ws")
        assert resp.status_code == 200
        assert resp.json()["unregistered"] is True
        assert resp.json()["path"] == "ws"

    def test_delete_not_found(self) -> None:
        _mock_registry.unregister_workspace.return_value = False
        resp = client.delete("/api/v2/registry/workspaces/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Memory: GET /api/v2/registry/memories
# ---------------------------------------------------------------------------


class TestListMemories:
    """Tests for GET /api/v2/registry/memories."""

    @patch("nexus.server.api.v2.routers.workspace._list_memory_db_models")
    def test_empty_list(self, mock_list: MagicMock) -> None:
        mock_list.return_value = []
        resp = client.get("/api/v2/registry/memories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["count"] == 0

    @patch("nexus.server.api.v2.routers.workspace._list_memory_db_models")
    def test_list_with_items(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            _make_mem_db_model("/mem1", name="M1"),
            _make_mem_db_model("/mem2", name="M2"),
        ]
        resp = client.get("/api/v2/registry/memories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2


# ---------------------------------------------------------------------------
# Memory: POST /api/v2/registry/memories
# ---------------------------------------------------------------------------


class TestRegisterMemory:
    """Tests for POST /api/v2/registry/memories."""

    @patch("nexus.server.api.v2.routers.workspace._get_memory_db_model")
    def test_register_success(self, mock_get_db: MagicMock) -> None:
        config = MemoryConfig(path="/new-mem", name="KB", created_at=datetime.now())
        _mock_registry.register_memory.return_value = config
        mock_get_db.return_value = _make_mem_db_model("/new-mem", name="KB")

        resp = client.post(
            "/api/v2/registry/memories",
            json={"path": "/new-mem", "name": "KB"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["path"] == "/new-mem"
        assert data["name"] == "KB"

    def test_register_duplicate_returns_409(self) -> None:
        _mock_registry.register_memory.side_effect = ValueError("already registered")
        resp = client.post(
            "/api/v2/registry/memories",
            json={"path": "/existing"},
        )
        assert resp.status_code == 409

    def test_register_validation_empty_path(self) -> None:
        resp = client.post(
            "/api/v2/registry/memories",
            json={"path": ""},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Memory: GET /api/v2/registry/memories/{path:path}
# ---------------------------------------------------------------------------


class TestGetMemory:
    """Tests for GET /api/v2/registry/memories/{path}."""

    @patch("nexus.server.api.v2.routers.workspace._get_memory_db_model")
    def test_get_found(self, mock_get_db: MagicMock) -> None:
        _mock_registry.get_memory.return_value = MemoryConfig(path="/mem")
        mock_get_db.return_value = _make_mem_db_model("/mem")

        resp = client.get("/api/v2/registry/memories/mem")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/mem"

    def test_get_not_found(self) -> None:
        _mock_registry.get_memory.return_value = None
        resp = client.get("/api/v2/registry/memories/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Memory: PATCH /api/v2/registry/memories/{path:path}
# ---------------------------------------------------------------------------


class TestUpdateMemory:
    """Tests for PATCH /api/v2/registry/memories/{path}."""

    @patch("nexus.server.api.v2.routers.workspace._get_memory_db_model")
    def test_update_success(self, mock_get_db: MagicMock) -> None:
        updated = MemoryConfig(path="/mem", name="Updated")
        _mock_registry.update_memory.return_value = updated
        mock_get_db.return_value = _make_mem_db_model("/mem", name="Updated")

        resp = client.patch(
            "/api/v2/registry/memories/mem",
            json={"name": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_update_not_found(self) -> None:
        _mock_registry.update_memory.side_effect = ValueError("not found")
        resp = client.patch(
            "/api/v2/registry/memories/mem",
            json={"description": "X"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Memory: DELETE /api/v2/registry/memories/{path:path}
# ---------------------------------------------------------------------------


class TestUnregisterMemory:
    """Tests for DELETE /api/v2/registry/memories/{path}."""

    def test_delete_success(self) -> None:
        _mock_registry.unregister_memory.return_value = True
        resp = client.delete("/api/v2/registry/memories/mem")
        assert resp.status_code == 200
        assert resp.json()["unregistered"] is True

    def test_delete_not_found(self) -> None:
        _mock_registry.unregister_memory.return_value = False
        resp = client.delete("/api/v2/registry/memories/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Path edge cases (parametrized)
# ---------------------------------------------------------------------------


class TestPathEdgeCases:
    """Test {path:path} parameter with tricky paths."""

    @pytest.mark.parametrize(
        "url_path,expected_path",
        [
            ("my-workspace", "my-workspace"),
            ("my/nested/workspace", "my/nested/workspace"),
            ("home/user/data/project", "home/user/data/project"),
            ("a", "a"),
            ("workspace-2026", "workspace-2026"),
            ("data_dir/sub_dir", "data_dir/sub_dir"),
        ],
    )
    def test_workspace_path_variants(self, url_path: str, expected_path: str) -> None:
        _mock_registry.get_workspace.return_value = None
        resp = client.get(f"/api/v2/registry/workspaces/{url_path}")
        assert resp.status_code == 404
        # Verify the path was correctly parsed (appears in error detail)
        assert expected_path in resp.json()["detail"]

    @pytest.mark.parametrize(
        "url_path,expected_path",
        [
            ("my-memory", "my-memory"),
            ("my/nested/memory", "my/nested/memory"),
        ],
    )
    def test_memory_path_variants(self, url_path: str, expected_path: str) -> None:
        _mock_registry.get_memory.return_value = None
        resp = client.get(f"/api/v2/registry/memories/{url_path}")
        assert resp.status_code == 404
        assert expected_path in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Response model completeness
# ---------------------------------------------------------------------------


class TestResponseModelFields:
    """Verify all TUI-required fields are in responses."""

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_workspace_response_has_all_tui_fields(self, mock_get_db: MagicMock) -> None:
        _mock_registry.get_workspace.return_value = WorkspaceConfig(path="/ws")
        mock_get_db.return_value = _make_ws_db_model(
            "/ws",
            name="Test",
            scope="session",
            session_id="s1",
            user_id="alice",
            agent_id="agent-1",
            extra_metadata='{"key": "value"}',
        )

        resp = client.get("/api/v2/registry/workspaces/ws")
        assert resp.status_code == 200
        data = resp.json()

        # All TUI-required fields per Issue #2987
        required_fields = [
            "path",
            "name",
            "description",
            "created_at",
            "created_by",
            "user_id",
            "agent_id",
            "scope",
            "session_id",
            "expires_at",
            "metadata",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

        assert data["metadata"] == {"key": "value"}

    @patch("nexus.server.api.v2.routers.workspace._get_memory_db_model")
    def test_memory_response_has_all_tui_fields(self, mock_get_db: MagicMock) -> None:
        _mock_registry.get_memory.return_value = MemoryConfig(path="/mem")
        mock_get_db.return_value = _make_mem_db_model(
            "/mem",
            name="KB",
            scope="persistent",
        )

        resp = client.get("/api/v2/registry/memories/mem")
        assert resp.status_code == 200
        data = resp.json()

        required_fields = [
            "path",
            "name",
            "description",
            "created_at",
            "created_by",
            "user_id",
            "agent_id",
            "scope",
            "session_id",
            "expires_at",
            "metadata",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"
