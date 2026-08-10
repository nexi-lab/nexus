"""Unit tests for POST /api/v2/search/index skip surfacing (Issue #4566).

The daemon now reports documents whose ``file_paths`` projection row never
landed within the bounded wait. The route must fail closed on those —
HTTP 409 with the skipped paths in ``detail`` — instead of returning a
silent ``count=0`` the client cannot distinguish from success.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "rw"]],
    "is_admin": False,
}


class _Runner:
    async def call(self, work: Any) -> Any:
        return await work()


class _Registry:
    def runner_for(self, zone_id: str) -> _Runner:
        return _Runner()


def _build_app(daemon: Any) -> "FastAPI":
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.zone_registry = _Registry()
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app


def _make_daemon(index_result: Any) -> MagicMock:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(return_value=index_result)
    return daemon


_DOCS = {
    "documents": [
        {"id": "1", "text": "alpha", "path": "/a.md"},
        {"id": "2", "text": "zulu", "path": "/b.md"},
    ]
}


def test_all_indexed_returns_200_with_count() -> None:
    daemon = _make_daemon(SimpleNamespace(indexed=2, skipped=[]))
    with TestClient(_build_app(daemon)) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "indexed"
    assert body["count"] == 2
    # #4617: the 200 shape keys the zone as camelCase ``zoneId`` — the
    # pre-P12 public wire contract (the 409 detail below keeps its
    # post-#4566 snake_case shape, which shipped that way from day one).
    assert body["zoneId"] == "eng"


def test_skipped_documents_return_409_with_paths() -> None:
    daemon = _make_daemon(SimpleNamespace(indexed=1, skipped=["/b.md"]))
    with TestClient(_build_app(daemon)) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["skipped"] == ["/b.md"]
    assert detail["count"] == 1
    assert detail["zone_id"] == "eng"


def test_int_returning_daemon_double_still_gets_200() -> None:
    """Legacy/mocked daemons that return a bare int keep the old contract."""
    daemon = _make_daemon(2)
    with TestClient(_build_app(daemon)) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 2


def test_daemon_error_returns_500() -> None:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(side_effect=RuntimeError("pipeline failed"))
    with TestClient(_build_app(daemon)) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)
    assert response.status_code == 500, response.text
    assert "pipeline failed" in response.json()["detail"]
