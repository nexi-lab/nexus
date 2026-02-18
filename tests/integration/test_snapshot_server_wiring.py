"""Snapshot server wiring integration test (Issue #1752).

Validates that the TransactionalSnapshotService is correctly wired
from factory -> NexusFS -> app.state -> REST API endpoints.

Uses an in-process FastAPI TestClient (no subprocess needed).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.metadata import FileMetadata
from nexus.server.api.v2.routers.snapshots import router as snapshots_router
from nexus.services.transactional_snapshot import TransactionalSnapshotService
from nexus.storage.models._base import Base
from nexus.storage.models.transactional_snapshot import TransactionSnapshotModel  # noqa: F401

# ---------------------------------------------------------------------------
# In-memory metadata store
# ---------------------------------------------------------------------------


class _InMemoryMetadataStore:
    """Minimal metadata store for integration tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, meta: FileMetadata) -> None:
        self._store[meta.path] = meta

    def get_batch(self, paths: list[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def put_batch(self, metadata_list: list[FileMetadata]) -> None:
        for meta in metadata_list:
            self._store[meta.path] = meta

    def delete_batch(self, paths: list[str]) -> None:
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
def wired_app():
    """Create a fully-wired FastAPI app with snapshot service on app.state."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    store = _InMemoryMetadataStore()

    # Pre-populate some files
    for i in range(5):
        store.put(_make_file(f"/workspace/file-{i}.txt", f"original-hash-{i}"))

    svc = TransactionalSnapshotService(
        metadata_store=store,
        session_factory=session_factory,
    )

    app = FastAPI()
    # Wire the service onto app.state — this is what _startup_transactional_snapshot does
    app.state.transactional_snapshot_service = svc
    # Router already has prefix="/api/v2/snapshots" baked in
    app.include_router(snapshots_router)

    return TestClient(app), store


# ---------------------------------------------------------------------------
# Tests: Full lifecycle validation
# ---------------------------------------------------------------------------


class TestSnapshotServerWiring:
    """Validate full REST API lifecycle with real service wiring."""

    def test_begin_get_commit_lifecycle(self, wired_app):
        """Full lifecycle: begin -> get -> commit via REST API."""
        client, _store = wired_app

        # BEGIN
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-1", "paths": ["/workspace/file-0.txt"]},
        )
        assert resp.status_code == 200, f"begin failed: {resp.text}"
        snapshot_id = resp.json()["snapshot_id"]
        assert len(snapshot_id) == 36  # UUID

        # GET
        resp = client.get(f"/api/v2/snapshots/{snapshot_id}")
        assert resp.status_code == 200
        info = resp.json()
        assert info["snapshot_id"] == snapshot_id
        assert info["agent_id"] == "agent-1"
        assert info["status"] == "ACTIVE"
        assert info["paths"] == ["/workspace/file-0.txt"]

        # COMMIT
        resp = client.post(f"/api/v2/snapshots/{snapshot_id}/commit")
        assert resp.status_code == 204

        # Verify committed
        resp = client.get(f"/api/v2/snapshots/{snapshot_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "COMMITTED"

    def test_begin_modify_rollback_lifecycle(self, wired_app):
        """Full lifecycle: begin -> modify files -> rollback restores originals."""
        client, store = wired_app

        paths = [f"/workspace/file-{i}.txt" for i in range(3)]

        # BEGIN
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "agent-2", "paths": paths},
        )
        assert resp.status_code == 200
        snapshot_id = resp.json()["snapshot_id"]

        # Modify files (simulate agent writes)
        for i in range(3):
            store.put(_make_file(f"/workspace/file-{i}.txt", f"modified-hash-{i}"))

        # Verify files are modified
        for i in range(3):
            meta = store.get(f"/workspace/file-{i}.txt")
            assert meta.etag == f"modified-hash-{i}"

        # ROLLBACK
        resp = client.post(f"/api/v2/snapshots/{snapshot_id}/rollback")
        assert resp.status_code == 200
        result = resp.json()
        assert result["snapshot_id"] == snapshot_id
        assert len(result["reverted"]) == 3
        assert result["stats"]["paths_reverted"] == 3

        # Verify files are restored to originals
        for i in range(3):
            meta = store.get(f"/workspace/file-{i}.txt")
            assert meta.etag == f"original-hash-{i}", f"file-{i} not restored"

        # Verify transaction is ROLLED_BACK
        resp = client.get(f"/api/v2/snapshots/{snapshot_id}")
        assert resp.json()["status"] == "ROLLED_BACK"

    def test_list_active_transactions(self, wired_app):
        """List active transactions for an agent."""
        client, _store = wired_app

        # Create two snapshots on different paths
        client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "list-agent", "paths": ["/workspace/file-0.txt"]},
        )
        client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "list-agent", "paths": ["/workspace/file-1.txt"]},
        )

        resp = client.get("/api/v2/snapshots/active", params={"agent_id": "list-agent"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert len(data["transactions"]) == 2

    def test_cleanup_expired(self, wired_app):
        """Cleanup endpoint returns count."""
        client, _store = wired_app

        resp = client.post("/api/v2/snapshots/cleanup")
        assert resp.status_code == 200
        assert "expired_count" in resp.json()

    def test_error_cases(self, wired_app):
        """Error responses are correct."""
        client, _store = wired_app

        # Empty paths -> 400
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "err-agent", "paths": []},
        )
        assert resp.status_code == 400

        # Non-existent snapshot -> 404
        resp = client.get("/api/v2/snapshots/nonexistent-id")
        assert resp.status_code == 404

        resp = client.post("/api/v2/snapshots/nonexistent-id/commit")
        assert resp.status_code == 404

        resp = client.post("/api/v2/snapshots/nonexistent-id/rollback")
        assert resp.status_code == 404

    def test_double_commit_returns_409(self, wired_app):
        """Committing twice returns 409."""
        client, _store = wired_app

        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "dbl-agent", "paths": ["/workspace/file-0.txt"]},
        )
        sid = resp.json()["snapshot_id"]

        # First commit succeeds
        resp = client.post(f"/api/v2/snapshots/{sid}/commit")
        assert resp.status_code == 204

        # Second commit fails with 409
        resp = client.post(f"/api/v2/snapshots/{sid}/commit")
        assert resp.status_code == 409

    def test_overlapping_paths_returns_409(self, wired_app):
        """Two active snapshots with overlapping paths returns 409."""
        client, _store = wired_app

        client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "overlap-agent", "paths": ["/workspace/file-0.txt"]},
        )

        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "overlap-agent", "paths": ["/workspace/file-0.txt"]},
        )
        assert resp.status_code == 409


class TestSnapshotServiceUnavailable:
    """Verify 503 when service is not wired."""

    def test_returns_503_when_service_is_none(self):
        app = FastAPI()
        app.state.transactional_snapshot_service = None
        app.include_router(snapshots_router)
        client = TestClient(app)

        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "x", "paths": ["/a"]},
        )
        assert resp.status_code == 503


class TestSnapshotPerformance:
    """Verify no performance regressions in the server wiring path."""

    def test_begin_commit_under_50ms(self, wired_app):
        """Single begin+commit roundtrip completes under 50ms."""
        client, _store = wired_app

        start = time.perf_counter()
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "perf-agent", "paths": ["/workspace/file-0.txt"]},
        )
        sid = resp.json()["snapshot_id"]
        client.post(f"/api/v2/snapshots/{sid}/commit")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 50, f"begin+commit took {elapsed_ms:.2f}ms (>50ms)"

    def test_100_file_rollback_under_200ms(self, wired_app):
        """Rollback of 100 files through REST API under 200ms."""
        client, store = wired_app

        # Add 100 files
        paths = []
        for i in range(100):
            path = f"/perf/file-{i}.txt"
            store.put(_make_file(path, f"orig-{i}"))
            paths.append(path)

        # Begin
        resp = client.post(
            "/api/v2/snapshots/begin",
            json={"agent_id": "perf-rollback", "paths": paths},
        )
        sid = resp.json()["snapshot_id"]

        # Modify all
        for i in range(100):
            store.put(_make_file(f"/perf/file-{i}.txt", f"mod-{i}"))

        # Rollback and measure
        start = time.perf_counter()
        resp = client.post(f"/api/v2/snapshots/{sid}/rollback")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert resp.status_code == 200
        assert resp.json()["stats"]["paths_reverted"] == 100
        assert elapsed_ms < 200, f"100-file rollback took {elapsed_ms:.2f}ms (>200ms)"
