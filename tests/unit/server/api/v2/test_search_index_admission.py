"""POST /api/v2/search/index admission control (#4777).

The plugin serializes index batches per zone, so every request past the
first waits on a mutex while holding an HTTP connection for minutes.  The
route now sheds above ``NEXUS_SEARCH_INDEX_MAX_INFLIGHT`` with 503 +
``Retry-After`` so clients back off instead of deepening the queue.
"""

from __future__ import annotations

import asyncio
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
    "is_admin": True,
}

_DOCS = {"documents": [{"id": "1", "text": "alpha", "path": "/a.md"}]}


class _Runner:
    async def call(self, work: Any) -> Any:
        return await work()


class _Registry:
    def runner_for(self, zone_id: str) -> _Runner:
        return _Runner()


def _build_app(daemon: Any) -> FastAPI:
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.zone_registry = _Registry()
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app


def _make_daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(return_value=SimpleNamespace(indexed=1, skipped=[]))
    return daemon


def test_default_cap_is_positive_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.server.api.v2.routers import search as search_mod

    monkeypatch.delenv(search_mod.INDEX_MAX_INFLIGHT_ENV, raising=False)
    assert search_mod.index_max_inflight() == search_mod.DEFAULT_INDEX_MAX_INFLIGHT
    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "3")
    assert search_mod.index_max_inflight() == 3
    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "not-an-int")
    assert search_mod.index_max_inflight() == search_mod.DEFAULT_INDEX_MAX_INFLIGHT
    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "-4")
    assert search_mod.index_max_inflight() == 0


def test_sheds_with_503_and_retry_after_when_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.server.api.v2.routers import search as search_mod

    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "2")
    daemon = _make_daemon()
    app = _build_app(daemon)
    # Two requests already parked on the plugin's zone mutex.
    app.state.search_index_inflight = 2

    with TestClient(app) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(search_mod.INDEX_RETRY_AFTER_SECONDS)
    detail = response.json()["detail"]
    assert detail["inflight"] == 2
    assert detail["max_inflight"] == 2
    assert "search/stats" in detail["hint"]
    # Shed requests must not touch the plugin or the counter.
    daemon.index_documents.assert_not_awaited()
    assert app.state.search_index_inflight == 2


def test_admitted_request_releases_its_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.server.api.v2.routers import search as search_mod

    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "2")
    daemon = _make_daemon()
    app = _build_app(daemon)
    app.state.search_index_inflight = 1

    with TestClient(app) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert app.state.search_index_inflight == 1


def test_slot_is_released_when_the_plugin_fails() -> None:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(side_effect=RuntimeError("plugin down"))
    app = _build_app(daemon)

    with TestClient(app) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)

    assert response.status_code == 500
    assert app.state.search_index_inflight == 0


def test_counter_tracks_concurrent_inflight_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """While a request is awaiting the plugin the slot stays reserved."""
    from nexus.server.api.v2.routers import search as search_mod

    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "1")
    observed: list[int] = []
    app_holder: dict[str, FastAPI] = {}

    async def _slow_index(documents: Any, *, zone_id: str) -> Any:
        observed.append(app_holder["app"].state.search_index_inflight)
        await asyncio.sleep(0)
        return SimpleNamespace(indexed=len(documents), skipped=[])

    daemon = MagicMock()
    daemon.index_documents = _slow_index
    app = _build_app(daemon)
    app_holder["app"] = app

    with TestClient(app) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)

    assert response.status_code == 200
    assert observed == [1]
    assert app.state.search_index_inflight == 0


def test_zero_cap_disables_admission_control(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.server.api.v2.routers import search as search_mod

    monkeypatch.setenv(search_mod.INDEX_MAX_INFLIGHT_ENV, "0")
    app = _build_app(_make_daemon())
    app.state.search_index_inflight = 10_000

    with TestClient(app) as client:
        response = client.post("/api/v2/search/index", json=_DOCS)

    assert response.status_code == 200
    assert app.state.search_index_inflight == 10_000
