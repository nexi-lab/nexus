"""Tests for /api/v2/path-contexts router (Issue #3773)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.bricks.search.path_context import PathContextStore
from nexus.server.api.v2.routers.path_contexts import router as path_contexts_router

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


@pytest_asyncio.fixture
async def test_app():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = PathContextStore(async_session_factory=factory, db_type="sqlite")

    app = FastAPI()
    app.state.path_context_store = store
    app.include_router(path_contexts_router)

    from nexus.server.api.v2.routers.path_contexts import _get_store
    from nexus.server.dependencies import require_admin, require_auth

    # Bypass loop-local resolution; always use the fixture's store.
    async def _override_store() -> PathContextStore:
        return store

    app.dependency_overrides[_get_store] = _override_store
    app.dependency_overrides[require_auth] = lambda: {
        "subject_id": "tester",
        "zone_id": "root",
        "is_admin": False,
    }
    app.dependency_overrides[require_admin] = lambda: {
        "subject_id": "admin",
        "zone_id": "root",
        "is_admin": True,
    }
    yield app
    await engine.dispose()


@pytest.fixture
def client(test_app: FastAPI) -> TestClient:
    return TestClient(test_app)


class TestPathContextRouter:
    def test_put_upsert_then_list(self, client: TestClient) -> None:
        r = client.put(
            "/api/v2/path-contexts/",
            json={
                "zone_id": "root",
                "path_prefix": "src/nexus/bricks/search",
                "description": "Search brick",
            },
        )
        assert r.status_code == 200, r.text
        r = client.get("/api/v2/path-contexts/", params={"zone_id": "root"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["contexts"]) == 1
        assert body["contexts"][0]["description"] == "Search brick"

    def test_put_replaces(self, client: TestClient) -> None:
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src", "description": "first"},
        )
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src", "description": "second"},
        )
        body = client.get("/api/v2/path-contexts/").json()
        assert len(body["contexts"]) == 1
        assert body["contexts"][0]["description"] == "second"

    def test_delete_removes(self, client: TestClient) -> None:
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src", "description": "x"},
        )
        r = client.delete(
            "/api/v2/path-contexts/",
            params={"zone_id": "root", "path_prefix": "src"},
        )
        assert r.status_code == 200
        body = client.get("/api/v2/path-contexts/").json()
        assert body["contexts"] == []

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        r = client.delete(
            "/api/v2/path-contexts/",
            params={"zone_id": "root", "path_prefix": "nonexistent"},
        )
        assert r.status_code == 404

    def test_put_normalizes_prefix(self, client: TestClient) -> None:
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "/src/", "description": "x"},
        )
        body = client.get("/api/v2/path-contexts/").json()
        assert body["contexts"][0]["path_prefix"] == "src"

    def test_put_rejects_traversal(self, client: TestClient) -> None:
        r = client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src/../etc", "description": "x"},
        )
        assert r.status_code == 422 or r.status_code == 400

    def test_non_admin_cannot_write(self, test_app: FastAPI) -> None:
        from fastapi import HTTPException

        from nexus.server.dependencies import require_admin

        def _reject() -> None:
            raise HTTPException(status_code=403, detail="admin required")

        test_app.dependency_overrides[require_admin] = _reject
        with TestClient(test_app) as c:
            r = c.put(
                "/api/v2/path-contexts/",
                json={"zone_id": "root", "path_prefix": "src", "description": "x"},
            )
            assert r.status_code == 403
            r = c.delete(
                "/api/v2/path-contexts/",
                params={"zone_id": "root", "path_prefix": "src"},
            )
            assert r.status_code == 403

    def test_list_requires_auth_only(self, client: TestClient) -> None:
        r = client.get("/api/v2/path-contexts/")
        assert r.status_code == 200

    def test_non_admin_rejected_for_other_zone_query(self, test_app: FastAPI) -> None:
        """Non-admin caller cannot pass zone_id different from their own
        (Issue #3773 review feedback)."""
        from nexus.server.dependencies import require_auth

        test_app.dependency_overrides[require_auth] = lambda: {
            "subject_id": "non-admin",
            "zone_id": "root",
            "is_admin": False,
        }
        with TestClient(test_app) as c:
            # Explicit foreign zone: 403.
            r = c.get("/api/v2/path-contexts/", params={"zone_id": "other"})
            assert r.status_code == 403
            # Own zone or implicit: allowed.
            assert c.get("/api/v2/path-contexts/", params={"zone_id": "root"}).status_code == 200
            assert c.get("/api/v2/path-contexts/").status_code == 200
