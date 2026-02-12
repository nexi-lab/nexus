"""Integration tests for Conflict REST API endpoints (Issue #1130).

Tests the full HTTP request/response cycle for conflict management
endpoints using a real ConflictLogStore backed by in-memory SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_conflict_log_store,
)
from nexus.server.api.v2.routers.conflicts import router
from nexus.services.conflict_log_store import ConflictLogStore
from nexus.services.conflict_resolution import (
    ConflictRecord,
    ConflictStrategy,
    ResolutionOutcome,
)
from nexus.storage.models import Base

# =============================================================================
# Helpers
# =============================================================================


def _now() -> datetime:
    return datetime.now(UTC)


def _make_record(
    *,
    record_id: str = "test-id-1",
    path: str = "/test/file.txt",
    backend_name: str = "gcs",
    zone_id: str = "default",
    strategy: ConflictStrategy = ConflictStrategy.KEEP_NEWER,
    outcome: ResolutionOutcome = ResolutionOutcome.NEXUS_WINS,
    status: str = "auto_resolved",
) -> ConflictRecord:
    now = _now()
    return ConflictRecord(
        id=record_id,
        path=path,
        backend_name=backend_name,
        zone_id=zone_id,
        strategy=strategy,
        outcome=outcome,
        nexus_content_hash="abc123",
        nexus_mtime=now,
        nexus_size=1024,
        backend_content_hash="def456",
        backend_mtime=now - timedelta(hours=1),
        backend_size=2048,
        conflict_copy_path=None,
        status=status,
        resolved_at=now,
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def store() -> ConflictLogStore:
    """ConflictLogStore backed by in-memory SQLite."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    gw = MagicMock()
    gw.session_factory = sessionmaker(bind=engine)
    return ConflictLogStore(gw)


@pytest.fixture
def client(store: ConflictLogStore) -> TestClient:
    """TestClient with conflicts router and real store."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return {"user": "test-user"}

    async def _mock_store():
        return store

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_conflict_log_store] = _mock_store

    return TestClient(app)


# =============================================================================
# GET /api/v2/sync/conflicts
# =============================================================================


class TestListConflicts:
    """Tests for GET /api/v2/sync/conflicts."""

    def test_list_empty(self, client: TestClient):
        """Returns empty list when no conflicts exist."""
        resp = client.get("/api/v2/sync/conflicts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["conflicts"] == []
        assert body["total"] == 0

    def test_list_returns_conflicts(self, client: TestClient, store: ConflictLogStore):
        """Returns stored conflicts."""
        store.log_conflict(_make_record(record_id="c1"))
        store.log_conflict(_make_record(record_id="c2"))

        resp = client.get("/api/v2/sync/conflicts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["conflicts"]) == 2

        ids = {c["conflict_id"] for c in body["conflicts"]}
        assert ids == {"c1", "c2"}

    def test_list_with_pagination(self, client: TestClient, store: ConflictLogStore):
        """Pagination limits results."""
        for i in range(5):
            store.log_conflict(_make_record(record_id=f"p-{i}"))

        resp = client.get("/api/v2/sync/conflicts?limit=2&offset=0")
        assert resp.status_code == 200
        assert len(resp.json()["conflicts"]) == 2

        resp2 = client.get("/api/v2/sync/conflicts?limit=2&offset=4")
        assert resp2.status_code == 200
        assert len(resp2.json()["conflicts"]) == 1

    def test_list_filter_by_status(self, client: TestClient, store: ConflictLogStore):
        """Filters by status."""
        store.log_conflict(_make_record(record_id="auto-1", status="auto_resolved"))
        store.log_conflict(_make_record(record_id="pending-1", status="manual_pending"))

        resp = client.get("/api/v2/sync/conflicts?status=manual_pending")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["conflicts"][0]["conflict_id"] == "pending-1"

    def test_list_filter_by_backend(self, client: TestClient, store: ConflictLogStore):
        """Filters by backend_name."""
        store.log_conflict(_make_record(record_id="gcs-1", backend_name="gcs"))
        store.log_conflict(_make_record(record_id="s3-1", backend_name="s3"))

        resp = client.get("/api/v2/sync/conflicts?backend_name=s3")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["conflicts"][0]["backend_name"] == "s3"


# =============================================================================
# GET /api/v2/sync/conflicts/{id}
# =============================================================================


class TestGetConflict:
    """Tests for GET /api/v2/sync/conflicts/{id}."""

    def test_get_found(self, client: TestClient, store: ConflictLogStore):
        """Returns conflict detail when found."""
        store.log_conflict(_make_record(record_id="found-1"))

        resp = client.get("/api/v2/sync/conflicts/found-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["conflict_id"] == "found-1"
        assert body["path"] == "/test/file.txt"
        assert body["strategy"] == "keep_newer"
        assert body["outcome"] == "nexus_wins"
        assert body["status"] == "auto_resolved"

    def test_get_not_found(self, client: TestClient):
        """Returns 404 for unknown conflict ID."""
        resp = client.get("/api/v2/sync/conflicts/nonexistent")
        assert resp.status_code == 404


# =============================================================================
# POST /api/v2/sync/conflicts/{id}/resolve
# =============================================================================


class TestResolveConflict:
    """Tests for POST /api/v2/sync/conflicts/{id}/resolve."""

    def test_resolve_pending_conflict(self, client: TestClient, store: ConflictLogStore):
        """Successfully resolves a manual_pending conflict."""
        store.log_conflict(_make_record(record_id="pending-1", status="manual_pending"))

        resp = client.post(
            "/api/v2/sync/conflicts/pending-1/resolve",
            json={"outcome": "nexus_wins"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["conflict_id"] == "pending-1"
        assert body["status"] == "manually_resolved"

        # Verify the store was updated
        record = store.get_conflict("pending-1")
        assert record is not None
        assert record.status == "manually_resolved"
        assert record.outcome == ResolutionOutcome.NEXUS_WINS

    def test_resolve_not_found(self, client: TestClient):
        """Returns 404 for unknown conflict ID."""
        resp = client.post(
            "/api/v2/sync/conflicts/ghost/resolve",
            json={"outcome": "backend_wins"},
        )
        assert resp.status_code == 404

    def test_resolve_already_resolved(self, client: TestClient, store: ConflictLogStore):
        """Returns 404 when conflict is not in manual_pending status."""
        store.log_conflict(_make_record(record_id="auto-1", status="auto_resolved"))

        resp = client.post(
            "/api/v2/sync/conflicts/auto-1/resolve",
            json={"outcome": "nexus_wins"},
        )
        assert resp.status_code == 404

    def test_resolve_invalid_outcome(self, client: TestClient, store: ConflictLogStore):
        """Returns 422 for invalid outcome value (Pydantic validation)."""
        store.log_conflict(_make_record(record_id="pending-2", status="manual_pending"))

        resp = client.post(
            "/api/v2/sync/conflicts/pending-2/resolve",
            json={"outcome": "invalid_value"},
        )
        assert resp.status_code == 422


# =============================================================================
# Auth Enforcement
# =============================================================================


@pytest.fixture
def client_no_auth(store: ConflictLogStore) -> TestClient:
    """TestClient where auth rejects all requests."""
    app = FastAPI()
    app.include_router(router)

    async def _reject_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _mock_store():
        return store

    app.dependency_overrides[_get_require_auth()] = _reject_auth
    app.dependency_overrides[get_conflict_log_store] = _mock_store

    return TestClient(app, raise_server_exceptions=False)


class TestAuthEnforcement:
    """Verify all endpoints reject unauthenticated requests."""

    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("GET", "/api/v2/sync/conflicts", None),
            ("GET", "/api/v2/sync/conflicts/some-id", None),
            ("POST", "/api/v2/sync/conflicts/some-id/resolve", {"outcome": "nexus_wins"}),
        ],
    )
    def test_rejects_unauthenticated(
        self,
        client_no_auth: TestClient,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
    ):
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        response = getattr(client_no_auth, method.lower())(path, **kwargs)
        assert response.status_code == 401


# =============================================================================
# Store Not Initialized (503)
# =============================================================================


@pytest.fixture
def client_no_store() -> TestClient:
    """TestClient where conflict_log_store is not available (write-back disabled)."""
    app = FastAPI()
    app.include_router(router)

    async def _mock_auth():
        return {"user": "test-user"}

    async def _no_store():
        raise HTTPException(status_code=503, detail="Conflict log store not initialized")

    app.dependency_overrides[_get_require_auth()] = _mock_auth
    app.dependency_overrides[get_conflict_log_store] = _no_store

    return TestClient(app, raise_server_exceptions=False)


class TestStoreNotInitialized:
    """Verify endpoints return 503 when write-back service is disabled."""

    def test_list_returns_503(self, client_no_store: TestClient):
        resp = client_no_store.get("/api/v2/sync/conflicts")
        assert resp.status_code == 503

    def test_get_returns_503(self, client_no_store: TestClient):
        resp = client_no_store.get("/api/v2/sync/conflicts/some-id")
        assert resp.status_code == 503

    def test_resolve_returns_503(self, client_no_store: TestClient):
        resp = client_no_store.post(
            "/api/v2/sync/conflicts/some-id/resolve",
            json={"outcome": "nexus_wins"},
        )
        assert resp.status_code == 503
