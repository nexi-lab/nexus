"""Unit tests for ``/api/v2/mcp/mounts`` router (Issue #3790).

Tests run against a FastAPI stub with a mocked MCPService — no live
nexus needed. Auth paths covered:
- no Authorization header → 401
- non-admin token → 403
- admin token via auth_provider → 201
- admin token via NEXUS_APPROVALS_ADMIN_TOKEN fallback → 201
- invalid body (missing both command and url) → handled by MCPService
  (ValidationError → 400 via global handler in production; here we
  assert the service was called and the surface didn't 422 on the
  schema)
- pydantic schema violations (missing name) → 422
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.exceptions import NexusError, ValidationError
from nexus.server.api.v2.routers.mcp import router as mcp_router
from nexus.server.error_handlers import nexus_error_handler


class _FakeAuthProvider:
    """Stand-in for the auth provider on app.state.

    Returns AuthResult-shaped objects so dependencies.resolve_auth treats
    them as the real thing.
    """

    def __init__(self, *, admin_tokens: dict[str, dict[str, Any]]) -> None:
        self._tokens = admin_tokens

    async def authenticate(self, token: str) -> Any:
        record = self._tokens.get(token)
        if record is None:
            return None
        return SimpleNamespace(
            authenticated=True,
            is_admin=record.get("is_admin", False),
            subject_type=record.get("subject_type", "user"),
            subject_id=record.get("subject_id", "u1"),
            zone_id=record.get("zone_id"),
            metadata={},
            inherit_permissions=True,
            zone_set=(),
            zone_perms=(),
        )


def _make_app(*, mcp_service: Any, auth_provider: Any = None) -> FastAPI:
    """Build a FastAPI app with the MCP router and a stubbed app.state."""
    app = FastAPI()

    # app.state setup mirroring NexusAppState — only the fields the
    # router and resolve_auth() touch.
    app.state.api_key = None
    app.state.auth_provider = auth_provider
    app.state.auth_cache_store = None
    app.state.nexus_fs = SimpleNamespace(
        service=lambda name: mcp_service if name == "mcp" else None
    )

    app.include_router(mcp_router)
    app.add_exception_handler(NexusError, nexus_error_handler)
    return app


def _client(app: FastAPI) -> TestClient:
    """TestClient with a non-loopback client host so open-access mode is
    NOT entered (resolve_auth treats loopback as open-access when no
    api_key/auth_provider is configured)."""
    return TestClient(app, base_url="http://example.com")


@pytest.fixture
def fake_mcp_service() -> AsyncMock:
    svc = AsyncMock()
    svc.mcp_mount.return_value = {
        "name": "test",
        "transport": "sse",
        "mounted": True,
        "tool_count": 0,
    }
    svc.mcp_list_mounts.return_value = [
        {
            "name": "existing",
            "description": "",
            "transport": "sse",
            "mounted": True,
            "tool_count": 3,
            "last_sync": None,
            "tools_path": "/mcp/existing/",
        },
    ]
    svc.mcp_unmount.return_value = {"success": True, "name": "existing"}
    return svc


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_post_without_auth_returns_401(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x", "transport": "sse", "url": "http://10.0.0.1:9999/sse"},
        )
    assert resp.status_code == 401, resp.text
    fake_mcp_service.mcp_mount.assert_not_called()


def test_post_with_non_admin_returns_403(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(
        admin_tokens={"non-admin-token": {"is_admin": False, "subject_id": "alice"}}
    )
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x", "transport": "sse", "url": "http://10.0.0.1:9999/sse"},
            headers={"Authorization": "Bearer non-admin-token"},
        )
    assert resp.status_code == 403, resp.text
    fake_mcp_service.mcp_mount.assert_not_called()


def test_post_with_admin_via_auth_provider_succeeds(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x", "transport": "sse", "url": "http://10.0.0.1:9999/sse"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 201, resp.text
    fake_mcp_service.mcp_mount.assert_awaited_once()
    body = resp.json()
    assert body["mounted"] is True


def test_post_with_approvals_admin_token_fallback(
    fake_mcp_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Issue #3790 fixture sets only NEXUS_APPROVALS_ADMIN_TOKEN —
    the router must still admit it."""
    monkeypatch.setenv("NEXUS_APPROVALS_ADMIN_TOKEN", "approvals-admin-secret")
    # NO auth_provider (closer to the fixture path).
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=None)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x", "transport": "sse", "url": "http://10.0.0.1:9999/sse"},
            headers={"Authorization": "Bearer approvals-admin-secret"},
        )
    assert resp.status_code == 201, resp.text
    fake_mcp_service.mcp_mount.assert_awaited_once()


def test_post_with_wrong_approvals_token_rejected(
    fake_mcp_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bearer that doesn't match NEXUS_APPROVALS_ADMIN_TOKEN must NOT
    be admitted as admin via the fallback. Under TestClient the request
    appears as loopback so resolve_auth grants open-access authentication
    (not admin); the fallback check then misses → 403 admin-required.
    The mount must not be called."""
    monkeypatch.setenv("NEXUS_APPROVALS_ADMIN_TOKEN", "right-secret")
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=None)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x", "transport": "sse", "url": "http://10.0.0.1:9999/sse"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
    assert resp.status_code == 403, resp.text
    fake_mcp_service.mcp_mount.assert_not_called()


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def test_post_missing_name_returns_422(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"transport": "sse", "url": "http://10.0.0.1:9999/sse"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 422, resp.text
    fake_mcp_service.mcp_mount.assert_not_called()


def test_post_missing_command_and_url_propagates_validation_error(
    fake_mcp_service: AsyncMock,
) -> None:
    """MCPService.mcp_mount raises ValidationError when both command and
    url are missing; the global handler maps it to 400."""
    fake_mcp_service.mcp_mount.side_effect = ValidationError("Either command or url is required")
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 400, resp.text
    fake_mcp_service.mcp_mount.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET / DELETE happy paths
# ---------------------------------------------------------------------------


def test_get_mounts_admin(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/mcp/mounts",
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["mounts"][0]["name"] == "existing"


def test_delete_mount_admin(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.delete(
            "/api/v2/mcp/mounts/existing",
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_mcp_service.mcp_unmount.assert_awaited_once_with(name="existing", context=None)


def test_get_mounts_no_auth_returns_401(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.get("/api/v2/mcp/mounts")
    assert resp.status_code == 401, resp.text


def test_delete_mount_no_auth_returns_401(fake_mcp_service: AsyncMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(mcp_service=fake_mcp_service, auth_provider=auth)
    with _client(app) as client:
        resp = client.delete("/api/v2/mcp/mounts/x")
    assert resp.status_code == 401, resp.text


def test_post_when_mcp_service_unavailable_returns_503() -> None:
    """If app.state.nexus_fs.service('mcp') is None, return 503."""
    app = FastAPI()
    app.state.api_key = None
    app.state.auth_provider = _FakeAuthProvider(
        admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}}
    )
    app.state.auth_cache_store = None
    app.state.nexus_fs = SimpleNamespace(service=lambda name: None)
    app.include_router(mcp_router)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/mcp/mounts",
            json={"name": "x", "transport": "sse", "url": "http://10.0.0.1:9999/sse"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 503, resp.text
