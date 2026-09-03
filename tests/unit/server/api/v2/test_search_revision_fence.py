"""Search routes honour X-Nexus-Min-Revision before querying (Issue #4737)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.lib.zone_revision import (
    MIN_REVISION_HEADER,
    REVISION_HEADER,
    REVISION_TIMEOUT_HEADER,
    reset_zone_revision_cache,
)

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "r"]],
    "is_admin": False,
}

_LOCAL_GEN = 10


class FakeFS:
    def __init__(self) -> None:
        self.stat_calls: list[tuple[str, Any]] = []

    def sys_stat(self, path: str, context: Any = None) -> dict[str, Any]:
        self.stat_calls.append((path, context))
        return {"path": path, "gen": _LOCAL_GEN}

    def service(self, name: str) -> Any:
        return None


class _Runner:
    async def call(self, work: Any) -> Any:
        return await work()


class _Registry:
    def runner_for(self, zone_id: str) -> _Runner:
        return _Runner()


def _make_daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.is_initialized = True
    daemon.config = MagicMock()
    daemon.config.txtai_graph = False

    async def fake_search(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    daemon.search = AsyncMock(side_effect=fake_search)
    return daemon


def _build_app(fs: FakeFS) -> tuple[FastAPI, MagicMock]:
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    daemon = _make_daemon()
    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.record_store = object()
    app.state.async_read_session_factory = object()
    app.state.permission_enforcer = None
    app.state.zone_registry = _Registry()
    app.state.nexus_fs = fs
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app, daemon


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reset_zone_revision_cache()


def test_query_without_fence_does_not_stat() -> None:
    fs = FakeFS()
    app, daemon = _build_app(fs)

    with TestClient(app) as client:
        resp = client.get("/api/v2/search/query", params={"q": "hello"})

    assert resp.status_code == 200, resp.text
    assert REVISION_HEADER not in resp.headers
    assert fs.stat_calls == []
    daemon.search.assert_called_once()


def test_query_with_satisfied_fence_runs_and_stamps_revision() -> None:
    fs = FakeFS()
    app, daemon = _build_app(fs)

    with TestClient(app) as client:
        resp = client.get(
            "/api/v2/search/query",
            params={"q": "hello"},
            headers={MIN_REVISION_HEADER: "/ws/a.txt@4"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers[REVISION_HEADER] == f"/ws/a.txt@{_LOCAL_GEN}"
    # Fence stat ran under the caller's OperationContext (eng zone).
    assert fs.stat_calls[0][0] == "/ws/a.txt"
    assert getattr(fs.stat_calls[0][1], "zone_id", None) == "eng"
    daemon.search.assert_called_once()


def test_query_behind_revision_is_412_and_never_queries() -> None:
    app, daemon = _build_app(FakeFS())

    with TestClient(app) as client:
        resp = client.get(
            "/api/v2/search/query",
            params={"q": "hello"},
            headers={MIN_REVISION_HEADER: "/ws/a.txt@40", REVISION_TIMEOUT_HEADER: "0"},
        )

    assert resp.status_code == 412, resp.text
    assert resp.json()["detail"]["current_revision"] == f"/ws/a.txt@{_LOCAL_GEN}"
    assert resp.headers[REVISION_HEADER] == f"/ws/a.txt@{_LOCAL_GEN}"
    daemon.search.assert_not_called()


@pytest.mark.parametrize(
    ("method", "route", "kwargs"),
    [
        ("post", "/api/v2/search/query/batch", {"json": {"queries": [{"q": "hello"}]}}),
        ("get", "/api/v2/search/glob", {"params": {"pattern": "*.py"}}),
        ("post", "/api/v2/search/glob", {"json": {"pattern": "*.py"}}),
        ("get", "/api/v2/search/grep", {"params": {"pattern": "TODO"}}),
        ("post", "/api/v2/search/grep", {"json": {"pattern": "TODO"}}),
    ],
)
def test_other_search_routes_fence_before_work(method: str, route: str, kwargs: dict) -> None:
    app, daemon = _build_app(FakeFS())

    with TestClient(app) as client:
        resp = client.request(
            method,
            route,
            headers={MIN_REVISION_HEADER: "/ws/a.txt@99", REVISION_TIMEOUT_HEADER: "0"},
            **kwargs,
        )

    assert resp.status_code == 412, resp.text
    assert resp.json()["detail"]["error"] == "revision_not_applied"
    daemon.search.assert_not_called()
    daemon.batch_search.assert_not_called()
