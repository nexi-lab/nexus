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
async def test_federated_legs_carry_per_zone_boosts(store: PathContextStore) -> None:
    # Multi-zone token auto-promotes to federated; each local leg must
    # carry ITS zone's weight rows — and only its own.
    await store.upsert("eng", "docs", "tier-1 docs", weight=5.0)
    daemon = _make_daemon()
    captured: dict[str, Any] = {}

    async def capture_search(request: Any) -> list[Any]:
        captured[request.zone_id] = request
        return []

    daemon.search = capture_search

    app = _build_app(daemon, store)
    rebac = MagicMock()
    rebac.list_accessible_zones = AsyncMock(return_value=["eng", "ops"])
    app.state.rebac_service = rebac
    app.state.federated_per_file_rebac = False

    from nexus.server.dependencies import require_auth

    app.dependency_overrides[require_auth] = lambda: {
        **_AUTH,
        "zone_set": ["eng", "ops"],
        "zone_perms": [["eng", "r"], ["ops", "r"]],
    }

    with TestClient(app) as client:
        response = client.get("/api/v2/search/query", params={"q": "needle"})

    assert response.status_code == 200, response.text
    assert captured["eng"].path_prefix_boosts == {"/docs/": 5.0}
    assert not captured["ops"].path_prefix_boosts


@pytest.mark.asyncio
async def test_weight_update_visible_on_next_query(
    store: PathContextStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the freshness window disabled the resolver checks the DB
    # fingerprint per query — an out-of-band upsert between queries must
    # be reflected, not served stale.
    monkeypatch.setenv("NEXUS_PATH_CONTEXT_FRESHNESS_SECONDS", "0")
    await store.upsert("eng", "docs", "tier-1", weight=2.0)
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store)) as client:
        client.get("/api/v2/search/query", params={"q": "needle"})
        await store.upsert("eng", "docs", "tier-1", weight=9.0)
        client.get("/api/v2/search/query", params={"q": "needle"})
    req = daemon.search.call_args.args[0]
    assert req.path_prefix_boosts == {"/docs/": 9.0}


@pytest.mark.asyncio
async def test_out_of_band_write_is_seen_once_the_freshness_window_expires(
    store: PathContextStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inside the window the fingerprint is trusted (no per-query DB round
    # trip); a write from another process shows up once it expires.
    import time

    monkeypatch.setenv("NEXUS_PATH_CONTEXT_FRESHNESS_SECONDS", "0.3")
    await store.upsert("eng", "docs", "tier-1", weight=2.0)
    daemon = _make_daemon()
    with TestClient(_build_app(daemon, store)) as client:
        client.get("/api/v2/search/query", params={"q": "needle"})
        await store.upsert("eng", "docs", "tier-1", weight=9.0)
        client.get("/api/v2/search/query", params={"q": "needle"})
        assert daemon.search.call_args.args[0].path_prefix_boosts == {"/docs/": 2.0}
        time.sleep(0.4)
        client.get("/api/v2/search/query", params={"q": "needle"})
    assert daemon.search.call_args.args[0].path_prefix_boosts == {"/docs/": 9.0}


@pytest.mark.asyncio
async def test_route_upsert_is_visible_on_the_next_query_inside_the_window(
    store: PathContextStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write through THIS process's path-contexts route invalidates the
    # search cache, so even a long freshness window never hides it.
    from nexus.server.api.v2.routers.path_contexts import router as pc_router
    from nexus.server.dependencies import require_admin

    monkeypatch.setenv("NEXUS_PATH_CONTEXT_FRESHNESS_SECONDS", "3600")
    await store.upsert("eng", "docs", "tier-1", weight=2.0)
    daemon = _make_daemon()
    app = _build_app(daemon, store)
    app.dependency_overrides[require_admin] = lambda: {**_AUTH, "is_admin": True}
    app.include_router(pc_router)
    with TestClient(app) as client:
        client.get("/api/v2/search/query", params={"q": "needle"})
        put = client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "eng", "path_prefix": "docs", "description": "tier-1", "weight": 9.0},
        )
        assert put.status_code == 200, put.text
        client.get("/api/v2/search/query", params={"q": "needle"})
        assert daemon.search.call_args.args[0].path_prefix_boosts == {"/docs/": 9.0}
        delete = client.delete(
            "/api/v2/path-contexts/", params={"zone_id": "eng", "path_prefix": "docs"}
        )
        assert delete.status_code == 200, delete.text
        client.get("/api/v2/search/query", params={"q": "needle"})
    assert not daemon.search.call_args.args[0].path_prefix_boosts
