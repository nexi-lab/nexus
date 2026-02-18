"""Tests for Transactional Snapshot REST API (Issue #1752).

Tests the FastAPI router with a real service against in-memory SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.metadata import FileMetadata
from nexus.server.api.v2.routers.snapshots import router
from nexus.services.transactional_snapshot import TransactionalSnapshotService
from nexus.storage.models._base import Base
from nexus.storage.models.transactional_snapshot import TransactionSnapshotModel  # noqa: F401

# ---------------------------------------------------------------------------
# In-memory metadata store
# ---------------------------------------------------------------------------


class InMemoryMetadataStore:
    """Dict-backed metadata store for router tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, meta: FileMetadata) -> None:
        self._store[meta.path] = meta

    def get_batch(self, paths):
        return {p: self._store.get(p) for p in paths}

    def put_batch(self, metadata_list):
        for meta in metadata_list:
            self._store[meta.path] = meta

    def delete_batch(self, paths):
        for p in paths:
            self._store.pop(p, None)


def _make_file(path: str, content_hash: str = "hash-default") -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=content_hash,
        size=100,
        etag=content_hash,
        modified_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def metadata_store() -> InMemoryMetadataStore:
    return InMemoryMetadataStore()


@pytest.fixture()
def app(metadata_store: InMemoryMetadataStore) -> FastAPI:
    """Create test FastAPI app with snapshot router."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    test_app = FastAPI()
    test_app.include_router(router)

    test_app.state.transactional_snapshot_service = TransactionalSnapshotService(
        metadata_store=metadata_store,
        session_factory=session_factory,
    )
    return test_app


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# TestBeginEndpoint
# ---------------------------------------------------------------------------


class TestBeginEndpoint:
    """POST /api/v2/snapshots/begin"""

    def test_begin_returns_snapshot_id(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/data.txt"))
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "snapshot_id" in data
        assert len(data["snapshot_id"]) == 36  # UUID

    def test_begin_empty_paths_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": []},
        )
        assert resp.status_code == 400

    def test_begin_overlap_returns_409(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/data.txt"))
        client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# TestCommitEndpoint
# ---------------------------------------------------------------------------


class TestCommitEndpoint:
    """POST /api/v2/snapshots/{id}/commit"""

    def test_commit_returns_204(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/data.txt"))
        begin_resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        sid = begin_resp.json()["snapshot_id"]

        resp = client.post(f"/api/v2/snapshots/{sid}/commit")
        assert resp.status_code == 204

    def test_commit_not_found_returns_404(self, client: TestClient) -> None:
        resp = client.post("/api/v2/snapshots/nonexistent-id/commit")
        assert resp.status_code == 404

    def test_commit_already_committed_returns_409(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/data.txt"))
        begin_resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        sid = begin_resp.json()["snapshot_id"]
        client.post(f"/api/v2/snapshots/{sid}/commit")

        resp = client.post(f"/api/v2/snapshots/{sid}/commit")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# TestRollbackEndpoint
# ---------------------------------------------------------------------------


class TestRollbackEndpoint:
    """POST /api/v2/snapshots/{id}/rollback"""

    def test_rollback_restores_and_returns_result(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/data.txt", "original"))
        begin_resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        sid = begin_resp.json()["snapshot_id"]

        # Modify file
        metadata_store.put(_make_file("/data.txt", "modified"))

        resp = client.post(f"/api/v2/snapshots/{sid}/rollback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["snapshot_id"] == sid
        assert "/data.txt" in data["reverted"]
        assert metadata_store.get("/data.txt").etag == "original"

    def test_rollback_not_found_returns_404(self, client: TestClient) -> None:
        resp = client.post("/api/v2/snapshots/nonexistent-id/rollback")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestGetEndpoint
# ---------------------------------------------------------------------------


class TestGetEndpoint:
    """GET /api/v2/snapshots/{id}"""

    def test_get_returns_transaction_info(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/data.txt"))
        begin_resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/data.txt"]},
        )
        sid = begin_resp.json()["snapshot_id"]

        resp = client.get(f"/api/v2/snapshots/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["snapshot_id"] == sid
        assert data["agent_id"] == "agent-a"
        assert data["status"] == "ACTIVE"
        assert data["paths"] == ["/data.txt"]

    def test_get_not_found_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v2/snapshots/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestListActiveEndpoint
# ---------------------------------------------------------------------------


class TestListActiveEndpoint:
    """GET /api/v2/snapshots/active"""

    def test_list_active(
        self, client: TestClient, metadata_store: InMemoryMetadataStore
    ) -> None:
        metadata_store.put(_make_file("/a.txt"))
        metadata_store.put(_make_file("/b.txt"))
        client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/a.txt"]},
        )
        client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-a", "paths": ["/b.txt"]},
        )

        resp = client.get("/api/v2/snapshots/active", params={"agent_id": "agent-a"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert len(data["transactions"]) == 2

    def test_list_active_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v2/snapshots/active", params={"agent_id": "agent-x"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# TestCleanupEndpoint
# ---------------------------------------------------------------------------


class TestCleanupEndpoint:
    """POST /api/v2/snapshots/cleanup"""

    def test_cleanup_returns_count(self, client: TestClient) -> None:
        resp = client.post("/api/v2/snapshots/cleanup")
        assert resp.status_code == 200
        assert resp.json()["expired_count"] == 0


# ---------------------------------------------------------------------------
# TestServiceUnavailable
# ---------------------------------------------------------------------------


class TestServiceUnavailable:
    """503 when service is not wired."""

    def test_returns_503_when_no_service(self) -> None:
        no_svc_app = FastAPI()
        no_svc_app.include_router(router)
        # Don't set app.state.transactional_snapshot_service
        with TestClient(no_svc_app) as c:
            resp = c.post(
                "/api/v2/snapshots/begin",
                json={"agent_id": "a", "paths": ["/x"]},
            )
            assert resp.status_code == 503
