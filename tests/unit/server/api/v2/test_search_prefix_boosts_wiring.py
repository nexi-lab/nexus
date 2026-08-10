"""GET /api/v2/search/query must thread path_contexts weights to the daemon (#4620).

Post-P12 the plugin honours ``QueryRequest.path_prefix_boosts`` but the
server never populated it — path-context weight rows persisted via CRUD
and then silently never reached ranking. These tests pin the wiring:
rows with weights land on ``SearchRequest.path_prefix_boosts`` in the
plugin's expected key shape, and deployments without a store keep
searching unboosted.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.bricks.search.path_context import PathContextStore

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

_CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    weight FLOAT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


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


def _build_app(daemon: Any, store: PathContextStore | None) -> "FastAPI":
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.record_store = object()
    app.state.async_read_session_factory = object()
    app.state.permission_enforcer = None
    app.state.zone_registry = _Registry()
    if store is not None:
        app.state.path_context_store = store
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield PathContextStore(async_session_factory=factory, db_type="sqlite")
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force _get_store's app.state.path_context_store fallback — a DB URL
    # in the environment would spin up a real engine instead.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)


@pytest.mark.asyncio
async def test_weighted_row_reaches_daemon_request(store: PathContextStore) -> None:
    await store.upsert("eng", "docs", "tier-1 docs", weight=5.0)
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store)) as client:
        response = client.get("/api/v2/search/query", params={"q": "needle"})
    assert response.status_code == 200, response.text
    req = daemon.search.call_args.args[0]
    assert req.path_prefix_boosts == {"/docs/": 5.0}


@pytest.mark.asyncio
async def test_description_only_rows_leave_request_unboosted(store: PathContextStore) -> None:
    await store.upsert("eng", "docs", "just a description")
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store)) as client:
        response = client.get("/api/v2/search/query", params={"q": "needle"})
    assert response.status_code == 200, response.text
    req = daemon.search.call_args.args[0]
    assert not req.path_prefix_boosts


@pytest.mark.asyncio
async def test_other_zone_rows_do_not_bleed(store: PathContextStore) -> None:
    await store.upsert("other-zone", "docs", "tier-1 docs", weight=5.0)
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store)) as client:
        response = client.get("/api/v2/search/query", params={"q": "needle"})
    assert response.status_code == 200, response.text
    req = daemon.search.call_args.args[0]
    assert not req.path_prefix_boosts


def test_missing_store_fails_open_to_unboosted_search() -> None:
    # No env DB URL, no app.state store: boost resolution must not take
    # down /search/query — the request goes through without boosts.
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store=None)) as client:
        response = client.get("/api/v2/search/query", params={"q": "needle"})
    assert response.status_code == 200, response.text
    req = daemon.search.call_args.args[0]
    assert not req.path_prefix_boosts


@pytest.mark.asyncio
async def test_weight_update_visible_on_next_query(store: PathContextStore) -> None:
    # The resolver caches per zone with a DB fingerprint check — an
    # upsert between queries must be reflected, not served stale.
    await store.upsert("eng", "docs", "tier-1", weight=2.0)
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store)) as client:
        client.get("/api/v2/search/query", params={"q": "needle"})
        await store.upsert("eng", "docs", "tier-1", weight=9.0)
        client.get("/api/v2/search/query", params={"q": "needle"})
    req = daemon.search.call_args.args[0]
    assert req.path_prefix_boosts == {"/docs/": 9.0}
