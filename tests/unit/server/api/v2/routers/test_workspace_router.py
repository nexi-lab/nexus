"""Unit tests for the workspace registry REST API (Issue #2987).

Tests 5 workspace endpoints using FastAPI TestClient with a mock WorkspaceRegistry.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.workspace.workspace_registry import WorkspaceConfig
from nexus.server.api.v2.routers.workspace import workspace_router

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


def _make_mock_registry() -> MagicMock:
    registry = MagicMock()
    registry._workspaces = {}
    registry.get_workspace.return_value = None
    registry.list_workspaces.return_value = []
    registry.unregister_workspace.return_value = False
    return registry


_mock_registry = _make_mock_registry()
_auth_result = {"authenticated": True, "subject_id": "test-user", "zone_id": "root"}


def _build_test_app() -> FastAPI:
    from nexus.server.api.v2.dependencies import get_workspace_registry
    from nexus.server.api.v2.routers.workspace import _get_require_auth

    app = FastAPI()
    app.include_router(workspace_router)
    app.dependency_overrides[get_workspace_registry] = lambda: _mock_registry
    app.dependency_overrides[_get_require_auth()] = lambda: _auth_result
    return app


_test_app = _build_test_app()
client = TestClient(_test_app)


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    _mock_registry.reset_mock()
    _mock_registry._workspaces = {}
    _mock_registry.get_workspace.return_value = None
    _mock_registry.list_workspaces.return_value = []
    _mock_registry.unregister_workspace.return_value = False
    _mock_registry.register_workspace.side_effect = None
    _mock_registry.update_workspace.side_effect = None


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
# GET /api/v2/registry/workspaces
# ---------------------------------------------------------------------------


class TestListWorkspaces:
    @patch("nexus.server.api.v2.routers.workspace._list_workspace_db_models")
    def test_empty_list(self, mock_list: MagicMock) -> None:
        mock_list.return_value = []
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    @patch("nexus.server.api.v2.routers.workspace._list_workspace_db_models")
    def test_list_with_items(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            _make_ws_db_model("/ws1", name="WS1"),
            _make_ws_db_model("/ws2", name="WS2"),
        ]
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    @patch("nexus.server.api.v2.routers.workspace._list_workspace_db_models")
    def test_list_includes_v050_fields(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            _make_ws_db_model("/ws", scope="session", session_id="s1", agent_id="a1"),
        ]
        resp = client.get("/api/v2/registry/workspaces")
        item = resp.json()["items"][0]
        assert item["scope"] == "session"
        assert item["session_id"] == "s1"
        assert item["agent_id"] == "a1"


# ---------------------------------------------------------------------------
# POST /api/v2/registry/workspaces
# ---------------------------------------------------------------------------


class TestRegisterWorkspace:
    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_register_success(self, mock_get_db: MagicMock) -> None:
        _mock_registry.register_workspace.return_value = WorkspaceConfig(
            path="/new-ws", name="New", created_at=datetime.now()
        )
        mock_get_db.return_value = _make_ws_db_model("/new-ws", name="New")
        resp = client.post("/api/v2/registry/workspaces", json={"path": "/new-ws", "name": "New"})
        assert resp.status_code == 201
        assert resp.json()["path"] == "/new-ws"

    def test_register_duplicate_returns_409(self) -> None:
        _mock_registry.register_workspace.side_effect = ValueError("already registered")
        resp = client.post("/api/v2/registry/workspaces", json={"path": "/existing"})
        assert resp.status_code == 409

    def test_register_relative_path_returns_422(self) -> None:
        resp = client.post("/api/v2/registry/workspaces", json={"path": "relative/path"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v2/registry/workspaces/{path:path}
# ---------------------------------------------------------------------------


class TestGetWorkspace:
    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_get_found(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = _make_ws_db_model("/ws")
        resp = client.get("/api/v2/registry/workspaces/ws")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/ws"

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_get_not_found(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = None
        resp = client.get("/api/v2/registry/workspaces/nonexistent")
        assert resp.status_code == 404

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_get_with_slashes(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = _make_ws_db_model("/my/nested/ws")
        resp = client.get("/api/v2/registry/workspaces/my/nested/ws")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PATCH /api/v2/registry/workspaces/{path:path}
# ---------------------------------------------------------------------------


class TestUpdateWorkspace:
    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_update_success(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = _make_ws_db_model("/ws", name="Updated")
        _mock_registry.update_workspace.return_value = WorkspaceConfig(path="/ws", name="Updated")
        resp = client.patch("/api/v2/registry/workspaces/ws", json={"name": "Updated"})
        assert resp.status_code == 200

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_update_not_found(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = None
        resp = client.patch("/api/v2/registry/workspaces/ws", json={"name": "X"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v2/registry/workspaces/{path:path}
# ---------------------------------------------------------------------------


class TestUnregisterWorkspace:
    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_delete_success(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = _make_ws_db_model("/ws")
        _mock_registry.unregister_workspace.return_value = True
        resp = client.delete("/api/v2/registry/workspaces/ws")
        assert resp.status_code == 200
        assert resp.json()["unregistered"] is True

    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_delete_not_found(self, mock_get_db: MagicMock) -> None:
        mock_get_db.return_value = None
        resp = client.delete("/api/v2/registry/workspaces/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Path edge cases
# ---------------------------------------------------------------------------


class TestPathEdgeCases:
    @pytest.mark.parametrize(
        "url_path",
        ["my-workspace", "my/nested/workspace", "a", "data_dir/sub_dir"],
    )
    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_path_variants_normalize(self, mock_get_db: MagicMock, url_path: str) -> None:
        mock_get_db.return_value = None
        resp = client.get(f"/api/v2/registry/workspaces/{url_path}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Response model completeness
# ---------------------------------------------------------------------------


class TestResponseModelFields:
    @patch("nexus.server.api.v2.routers.workspace._get_workspace_db_model")
    def test_workspace_response_has_all_tui_fields(self, mock_get_db: MagicMock) -> None:
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
        for field in [
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
        ]:
            assert field in data, f"Missing field: {field}"
        assert data["metadata"] == {"key": "value"}
