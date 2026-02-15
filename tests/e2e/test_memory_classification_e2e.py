"""E2E tests for Memory Temporal Stability Classification (#1191).

Tests the FastAPI endpoints for auto-classifying memories as
static, semi_dynamic, or dynamic at write time.

Uses FastAPI TestClient with DatabaseAPIKeyAuth for real auth testing,
following the pattern from test_memory_paging_fastapi_e2e.py.

Run with: python -m pytest tests/e2e/test_memory_classification_e2e.py -v
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol, PaginatedResult
from nexus.core.config import PermissionConfig
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.factory import DiscriminatingAuthProvider
from nexus.storage.models import Base

# ==============================================================================
# In-memory metadata store stub (avoids LocalRaft dependency)
# ==============================================================================


class InMemoryMetadataStore(FileMetadataProtocol):
    """Minimal in-memory metadata store for tests that don't need file ops."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, Any] | None:
        removed = self._store.pop(path, None)
        if removed:
            return {"path": path}
        return None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(  # noqa: A003
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]:
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        items = self.list(prefix, recursive)
        return PaginatedResult(
            items=items[:limit],
            next_cursor=None,
            has_more=len(items) > limit,
            total_count=len(items),
        )

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def close(self) -> None:
        self._store.clear()


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Set required env vars for server modules."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-12345")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)


@pytest.fixture
def db_engine(tmp_path):
    """Create file-backed SQLite engine shared across all sessions."""
    db_path = tmp_path / "test_classification.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session_factory(db_engine):
    """Create session factory bound to shared engine."""
    return sessionmaker(bind=db_engine)


@pytest.fixture
def api_keys(db_session_factory):
    """Create normal user API key via DatabaseAPIKeyAuth."""
    with db_session_factory() as session:
        normal_key_id, normal_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="test-user",
            name="Test User Key",
            zone_id="default",
            is_admin=False,
        )
        session.commit()

    return {
        "normal_key": normal_raw,
        "normal_key_id": normal_key_id,
    }


@pytest.fixture
def app_with_auth(tmp_path, db_session_factory, api_keys):
    """Create FastAPI app with DatabaseAPIKeyAuth."""
    from nexus.backends.local import LocalBackend
    from nexus.core.nexus_fs import NexusFS
    from nexus.server.fastapi_server import create_app
    from nexus.storage.record_store import SQLAlchemyRecordStore

    tmpdir = tempfile.mkdtemp(prefix="nexus-classification-e2e-")

    backend = LocalBackend(root_path=tmpdir)
    metadata_store = InMemoryMetadataStore()

    record_db_path = tmp_path / "records.db"
    record_store = SQLAlchemyRecordStore(db_path=str(record_db_path))

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=False),
    )

    db_key_provider = DatabaseAPIKeyAuth(session_factory=db_session_factory)
    auth_provider = DiscriminatingAuthProvider(
        api_key_provider=db_key_provider,
        jwt_provider=None,
    )

    app = create_app(
        nexus_fs=nx,
        auth_provider=auth_provider,
        database_url=f"sqlite:///{record_db_path}",
    )

    yield app

    metadata_store.close()
    record_store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(app_with_auth):
    """Create TestClient."""
    return TestClient(app_with_auth)


@pytest.fixture
def headers(api_keys):
    """Auth headers for normal user."""
    return {"Authorization": f"Bearer {api_keys['normal_key']}"}


# ==============================================================================
# Tests
# ==============================================================================


class TestMemoryClassificationE2E:
    """E2E tests for memory stability classification (#1191)."""

    def _store_memory(
        self,
        client: TestClient,
        headers: dict,
        content: str,
        classify_stability: bool = True,
        **kwargs,
    ) -> dict:
        """Helper to store a memory and return response data."""
        payload = {
            "content": content,
            "scope": "user",
            "classify_stability": classify_stability,
            **kwargs,
        }
        resp = client.post("/api/v2/memories", json=payload, headers=headers)
        assert resp.status_code == 201, f"Store failed: {resp.text}"
        return resp.json()

    def _get_memory(self, client: TestClient, headers: dict, memory_id: str) -> dict:
        """Helper to get a memory by ID."""
        resp = client.get(f"/api/v2/memories/{memory_id}", headers=headers)
        assert resp.status_code == 200, f"Get failed: {resp.text}"
        return resp.json()["memory"]

    def test_store_static_memory_classified(self, client, headers):
        """POST static memory -> temporal_stability = 'static'."""
        result = self._store_memory(client, headers, "Paris is the capital of France")
        memory = self._get_memory(client, headers, result["memory_id"])
        assert memory["temporal_stability"] == "static"
        assert memory["stability_confidence"] is not None
        assert memory["stability_confidence"] >= 0.5
        assert memory["estimated_ttl_days"] is None  # Static = infinite

    def test_store_dynamic_memory_classified(self, client, headers):
        """POST dynamic memory -> temporal_stability = 'dynamic'."""
        result = self._store_memory(client, headers, "John is currently working on the Q4 report")
        memory = self._get_memory(client, headers, result["memory_id"])
        assert memory["temporal_stability"] == "dynamic"
        assert memory["estimated_ttl_days"] is not None

    def test_store_semi_dynamic_memory_classified(self, client, headers):
        """POST semi-dynamic memory -> temporal_stability = 'semi_dynamic'."""
        result = self._store_memory(
            client, headers, "Sarah works at Microsoft as a senior engineer"
        )
        memory = self._get_memory(client, headers, result["memory_id"])
        assert memory["temporal_stability"] == "semi_dynamic"

    def test_query_with_temporal_stability_filter(self, client, headers):
        """POST query with temporal_stability filter -> correct filtering."""
        self._store_memory(client, headers, "Water has a boiling point of 100 degrees Celsius")
        self._store_memory(client, headers, "She is currently at the meeting right now")

        resp = client.post(
            "/api/v2/memories/query",
            json={"temporal_stability": "static"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        for r in data["results"]:
            assert r["temporal_stability"] == "static"

    def test_batch_with_classify_stability_false(self, client, headers):
        """POST batch with classify_stability=False -> fields are null."""
        resp = client.post(
            "/api/v2/memories/batch",
            json={
                "memories": [
                    {
                        "content": "Paris is the capital of France",
                        "scope": "user",
                        "classify_stability": False,
                    },
                    {
                        "content": "Currently raining outside",
                        "scope": "user",
                        "classify_stability": False,
                    },
                ]
            },
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["stored"] == 2

        for memory_id in data["memory_ids"]:
            memory = self._get_memory(client, headers, memory_id)
            assert memory["temporal_stability"] is None
            assert memory["stability_confidence"] is None
            assert memory["estimated_ttl_days"] is None

    def test_classification_fields_in_get_response(self, client, headers):
        """GET memory response includes classification fields."""
        result = self._store_memory(
            client,
            headers,
            "The Pythagorean theorem states a squared plus b squared equals c squared",
        )
        memory = self._get_memory(client, headers, result["memory_id"])
        assert "temporal_stability" in memory
        assert "stability_confidence" in memory
        assert "estimated_ttl_days" in memory

    def test_store_without_auth_returns_401(self, client):
        """POST /api/v2/memories without auth -> 401."""
        resp = client.post(
            "/api/v2/memories",
            json={
                "content": "Should fail",
                "scope": "user",
            },
        )
        assert resp.status_code == 401

    def test_query_dynamic_memories_only(self, client, headers):
        """Query for dynamic memories filters correctly."""
        self._store_memory(client, headers, "The speed of light is always 299792458 m/s")
        self._store_memory(client, headers, "She is presently reviewing the budget right now")

        resp = client.post(
            "/api/v2/memories/query",
            json={"temporal_stability": "dynamic"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        for r in data["results"]:
            assert r["temporal_stability"] == "dynamic"
