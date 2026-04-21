"""Integration tests for ReBAC filtering on POST /api/v2/search/locate (Issue #3725, 11A).

Verifies that the /locate endpoint respects file-level permissions:
    - A user without access to a path gets 0 results for that path.
    - A user with access gets expected results.
    - Missing permission_enforcer means no filtering (all results pass through).

Uses a mock daemon and mock permission enforcer to isolate endpoint logic from
infrastructure dependencies.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.routers.search import router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(
    daemon: Any,
    permission_enforcer: Any | None,
    zone_id: str = ROOT_ZONE_ID,
) -> FastAPI:
    """Build a minimal FastAPI app with the search router and mocked state."""
    app = FastAPI()
    app.include_router(router)
    app.state.search_daemon = daemon
    app.state.permission_enforcer = permission_enforcer
    # Inject auth so require_auth dependency resolves
    app.state._test_zone_id = zone_id
    return app


def _make_daemon(candidates: list[dict[str, Any]]) -> MagicMock:
    """Stub daemon whose locate() returns given candidates."""
    daemon = MagicMock()
    daemon.is_initialized = True
    daemon.locate = AsyncMock(return_value=candidates)
    return daemon


def _make_enforcer(permitted_paths: list[str]) -> MagicMock:
    """Stub enforcer that only allows paths in permitted_paths."""
    enforcer = MagicMock()

    def _filter(paths: list[str], *, user_id: str, zone_id: str, is_admin: bool) -> list[str]:
        return [p for p in paths if p in permitted_paths]

    enforcer.filter_search_results = MagicMock(side_effect=_filter)
    return enforcer


# Override require_auth to inject a test user without needing JWT infrastructure
def _auth_override(zone_id: str = ROOT_ZONE_ID) -> dict[str, Any]:
    return {"subject_id": "test-user", "zone_id": zone_id, "is_admin": False}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthorized_user_gets_zero_results() -> None:
    """User without access to any candidate path gets an empty candidates list."""
    candidates = [
        {"path": "/workspace/src/auth/login.py", "score": 8.4, "title": "Login module"},
        {"path": "/workspace/src/auth/oauth.py", "score": 6.2, "title": "OAuth middleware"},
    ]
    # Enforcer denies access to all paths
    enforcer = _make_enforcer(permitted_paths=[])
    daemon = _make_daemon(candidates)

    app = _make_app(daemon, enforcer)
    app.dependency_overrides[
        __import__("nexus.server.dependencies", fromlist=["require_auth"]).require_auth
    ] = lambda: _auth_override()

    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/v2/search/locate", json={"q": "authentication"})

    assert response.status_code == 200
    body = response.json()
    assert body["candidates"] == [], f"expected empty, got: {body['candidates']}"
    assert body["total_before_filter"] == 2
    assert body["permission_denial_rate"] == 1.0


@pytest.mark.asyncio
async def test_authorized_user_gets_permitted_results() -> None:
    """User with access to one of two candidate paths gets exactly that path."""
    login_path = "/workspace/src/auth/login.py"
    oauth_path = "/workspace/src/auth/oauth.py"

    candidates = [
        {"path": login_path, "score": 8.4, "title": "Login module"},
        {"path": oauth_path, "score": 6.2, "title": "OAuth middleware"},
    ]
    # Enforcer grants access only to login_path
    enforcer = _make_enforcer(permitted_paths=[login_path])
    daemon = _make_daemon(candidates)

    app = _make_app(daemon, enforcer)
    app.dependency_overrides[
        __import__("nexus.server.dependencies", fromlist=["require_auth"]).require_auth
    ] = lambda: _auth_override()

    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/v2/search/locate", json={"q": "authentication"})

    assert response.status_code == 200
    body = response.json()
    paths = [c["path"] for c in body["candidates"]]
    assert login_path in paths
    assert oauth_path not in paths


@pytest.mark.asyncio
async def test_no_enforcer_returns_all_results() -> None:
    """When no permission enforcer is configured, all results pass through."""
    candidates = [
        {"path": "/workspace/src/auth/login.py", "score": 8.4, "title": "Login module"},
    ]
    daemon = _make_daemon(candidates)

    app = _make_app(daemon, permission_enforcer=None)
    app.dependency_overrides[
        __import__("nexus.server.dependencies", fromlist=["require_auth"]).require_auth
    ] = lambda: _auth_override()

    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/v2/search/locate", json={"q": "login"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["candidates"]) == 1
    assert body["permission_denial_rate"] == 0.0


@pytest.mark.asyncio
async def test_missing_q_returns_400() -> None:
    """Empty or missing query returns HTTP 400."""
    daemon = _make_daemon([])
    app = _make_app(daemon, permission_enforcer=None)
    app.dependency_overrides[
        __import__("nexus.server.dependencies", fromlist=["require_auth"]).require_auth
    ] = lambda: _auth_override()

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/v2/search/locate", json={"q": ""})
    assert response.status_code == 400
