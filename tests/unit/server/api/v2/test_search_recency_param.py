"""Unit tests for the recency request params on GET /api/v2/search/query (#4543).

Covers:
(a) recency=on|auto accepted and threaded to daemon.search
(b) recency=bogus -> 400 before touching the daemon
(c) params omitted -> daemon receives None (defer-to-config sentinel)
(d) weight/half-life bounds -> 422 from FastAPI validation
"""

from __future__ import annotations

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
    "zone_perms": [["eng", "r"]],
    "is_admin": False,
}


class _RecordingRunner:
    async def call(self, work: Any) -> Any:
        return await work()


class _RecordingRegistry:
    def runner_for(self, zone_id: str) -> _RecordingRunner:
        return _RecordingRunner()


def _build_app(daemon: Any) -> "FastAPI":
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.record_store = object()
    app.state.async_read_session_factory = object()
    app.state.permission_enforcer = None
    app.state.zone_registry = _RecordingRegistry()
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app


def _make_daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.is_initialized = True
    daemon.config = MagicMock()
    daemon.config.txtai_graph = False

    async def fake_search(**kwargs: Any) -> list[Any]:
        return []

    daemon.search = AsyncMock(side_effect=fake_search)
    return daemon


def test_recency_bogus_returns_400() -> None:
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get("/api/v2/search/query", params={"q": "x", "recency": "bogus"})
    assert response.status_code == 400, response.text
    assert "recency" in response.json().get("detail", "").lower()
    daemon.search.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "on", "auto"])
def test_recency_mode_accepted_and_threaded(mode: str) -> None:
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get(
            "/api/v2/search/query",
            params={
                "q": "x",
                "recency": mode,
                "recency_weight": 0.5,
                "recency_half_life_days": 7,
            },
        )
    assert response.status_code not in (400, 422), response.text
    kwargs = daemon.search.call_args.kwargs
    assert kwargs.get("recency") == mode
    assert kwargs.get("recency_weight") == 0.5
    assert kwargs.get("recency_half_life_days") == 7.0


def test_recency_omitted_forwards_none() -> None:
    """None is the defer-to-DaemonConfig sentinel — must survive the router."""
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get("/api/v2/search/query", params={"q": "x"})
    assert response.status_code not in (400, 422), response.text
    kwargs = daemon.search.call_args.kwargs
    assert kwargs.get("recency") is None
    assert kwargs.get("recency_weight") is None
    assert kwargs.get("recency_half_life_days") is None


@pytest.mark.parametrize(
    "params",
    [
        {"recency_weight": -0.1},
        {"recency_weight": 5.1},
        {"recency_half_life_days": 0},
        {"recency_half_life_days": 3651},
    ],
)
def test_out_of_bounds_knobs_return_422(params: dict[str, Any]) -> None:
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get("/api/v2/search/query", params={"q": "x", **params})
    assert response.status_code == 422, response.text
    daemon.search.assert_not_called()
