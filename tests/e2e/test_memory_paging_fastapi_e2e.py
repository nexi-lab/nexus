"""FastAPI E2E tests for memory paging with authentication.

Tests the memory paging system through the HTTP API layer with
Bearer token authentication enabled, covering:
- Admin user (is_admin=True) operations
- Normal user (is_admin=False) operations
- Unauthenticated (no token) rejection
- Revoked key rejection
- Paging tier distribution

Run with: python -m pytest tests/e2e/test_memory_paging_fastapi_e2e.py -v
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
    """Set required env vars for server modules without polluting global state."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-12345")
    # Remove NEXUS_DATABASE_URL so RecordStore uses explicit db_path, not env override
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)


@pytest.fixture
def db_engine(tmp_path):
    """Create a file-backed SQLite engine shared across all sessions.

    Uses a temp file so that DatabaseAPIKeyAuth's session factory sees
    the same database as the fixture that creates API keys.
    """
    db_path = tmp_path / "test_auth.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)

    # Enable WAL mode for concurrent reads while writing
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
    """Create a session factory bound to the shared engine."""
    return sessionmaker(bind=db_engine)


@pytest.fixture
def api_keys(db_session_factory):
    """Create admin and normal user API keys via DatabaseAPIKeyAuth.

    Returns:
        dict with 'admin_key', 'normal_key', 'admin_key_id', 'normal_key_id'
    """
    with db_session_factory() as session:
        admin_key_id, admin_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="admin-user",
            name="Admin Key",
            zone_id="default",
            is_admin=True,
        )
        normal_key_id, normal_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="normal-user",
            name="Normal User Key",
            zone_id="default",
            is_admin=False,
        )
        session.commit()

    return {
        "admin_key": admin_raw,
        "admin_key_id": admin_key_id,
        "normal_key": normal_raw,
        "normal_key_id": normal_key_id,
    }


@pytest.fixture
def app_with_db_auth(tmp_path, db_session_factory, api_keys):
    """Create FastAPI app with DatabaseAPIKeyAuth and paging enabled."""
    from nexus.backends.local import LocalBackend
    from nexus.core.nexus_fs import NexusFS
    from nexus.server.fastapi_server import create_app
    from nexus.storage.record_store import SQLAlchemyRecordStore

    tmpdir = tempfile.mkdtemp(prefix="nexus-paging-auth-e2e-")

    backend = LocalBackend(root_path=tmpdir)
    metadata_store = InMemoryMetadataStore()

    # Use file-backed SQLite for RecordStore to avoid in-memory connection isolation
    record_db_path = tmp_path / "records.db"
    record_store = SQLAlchemyRecordStore(db_path=str(record_db_path))

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=False,
        enable_memory_paging=True,
        memory_main_capacity=10,
    )

    # Match production wiring: DiscriminatingAuthProvider routes sk-* tokens
    # to DatabaseAPIKeyAuth (same as `nexus serve --auth-type database`)
    db_key_provider = DatabaseAPIKeyAuth(session_factory=db_session_factory)
    auth_provider = DiscriminatingAuthProvider(
        api_key_provider=db_key_provider,
        jwt_provider=None,  # No JWT in this test
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
def client(app_with_db_auth):
    """Create TestClient."""
    return TestClient(app_with_db_auth)


@pytest.fixture
def admin_headers(api_keys):
    """Auth headers for admin user."""
    return {"Authorization": f"Bearer {api_keys['admin_key']}"}


@pytest.fixture
def normal_headers(api_keys):
    """Auth headers for normal (non-admin) user."""
    return {"Authorization": f"Bearer {api_keys['normal_key']}"}


# ==============================================================================
# Tests: Unauthenticated
# ==============================================================================


class TestUnauthenticated:
    """Unauthenticated requests should be rejected."""

    def test_store_without_auth_returns_401(self, client):
        """POST /api/v2/memories without token -> 401."""
        response = client.post(
            "/api/v2/memories",
            json={"content": "This should fail", "scope": "user"},
        )
        assert response.status_code == 401

    def test_search_without_auth_returns_401(self, client):
        """POST /api/v2/memories/search without token -> 401."""
        response = client.post(
            "/api/v2/memories/search",
            json={"query": "test", "limit": 5, "search_mode": "keyword"},
        )
        assert response.status_code == 401

    def test_query_without_auth_returns_401(self, client):
        """POST /api/v2/memories/query without token -> 401."""
        response = client.post(
            "/api/v2/memories/query",
            json={"memory_type": "fact", "limit": 10},
        )
        assert response.status_code == 401

    def test_invalid_token_returns_401(self, client):
        """Bearer token that doesn't exist in DB -> 401."""
        response = client.post(
            "/api/v2/memories",
            json={"content": "Bad token", "scope": "user"},
            headers={
                "Authorization": "Bearer sk-invalid_fake_key_00000000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
        )
        assert response.status_code == 401


# ==============================================================================
# Tests: Admin User
# ==============================================================================


class TestAdminUser:
    """Admin user (is_admin=True) operations."""

    def test_admin_store_memory(self, client, admin_headers):
        """Admin can store memories -> 201."""
        response = client.post(
            "/api/v2/memories",
            json={
                "content": "Admin stored: Paris is the capital of France",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.9,
            },
            headers=admin_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert "memory_id" in data
        assert data["status"] == "created"

    def test_admin_get_paging_stats(self, client, admin_headers):
        """Admin can view paging stats."""
        response = client.get("/api/v2/memories/stats", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert "paging_enabled" in data

    def test_admin_search_memories(self, client, admin_headers):
        """Admin can search memories."""
        client.post(
            "/api/v2/memories",
            json={
                "content": "The speed of light is 299792458 m/s",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.8,
            },
            headers=admin_headers,
        )

        response = client.post(
            "/api/v2/memories/search",
            json={"query": "speed of light", "limit": 5, "search_mode": "keyword"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "results" in data or "memories" in data or isinstance(data, list)

    def test_admin_query_memories(self, client, admin_headers):
        """Admin can query memories by type."""
        client.post(
            "/api/v2/memories",
            json={
                "content": "Admin prefers vim",
                "scope": "user",
                "memory_type": "preference",
                "importance": 0.7,
            },
            headers=admin_headers,
        )

        response = client.post(
            "/api/v2/memories/query",
            json={"memory_type": "preference", "limit": 10},
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, (dict, list))

    def test_admin_delete_memory(self, client, admin_headers):
        """Admin can delete memories."""
        store_resp = client.post(
            "/api/v2/memories",
            json={
                "content": "Temporary fact to delete",
                "scope": "user",
                "memory_type": "fact",
            },
            headers=admin_headers,
        )
        assert store_resp.status_code == 201
        memory_id = store_resp.json()["memory_id"]

        delete_resp = client.delete(
            f"/api/v2/memories/{memory_id}",
            headers=admin_headers,
        )
        assert delete_resp.status_code == 200
        assert delete_resp.json()["deleted"] is True


# ==============================================================================
# Tests: Normal User
# ==============================================================================


class TestNormalUser:
    """Normal user (is_admin=False) operations."""

    def test_normal_user_store_memory(self, client, normal_headers):
        """Normal user can store memories -> 201."""
        response = client.post(
            "/api/v2/memories",
            json={
                "content": "Normal user stored: Water boils at 100C",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.6,
            },
            headers=normal_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert "memory_id" in data
        assert data["status"] == "created"

    def test_normal_user_get_paging_stats(self, client, normal_headers):
        """Normal user can view paging stats."""
        response = client.get("/api/v2/memories/stats", headers=normal_headers)
        assert response.status_code == 200
        data = response.json()
        assert "paging_enabled" in data

    def test_normal_user_search_memories(self, client, normal_headers):
        """Normal user can search memories."""
        client.post(
            "/api/v2/memories",
            json={
                "content": "Gravity pulls objects toward earth",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.7,
            },
            headers=normal_headers,
        )

        response = client.post(
            "/api/v2/memories/search",
            json={"query": "gravity earth", "limit": 5, "search_mode": "keyword"},
            headers=normal_headers,
        )
        assert response.status_code == 200

    def test_normal_user_query_memories(self, client, normal_headers):
        """Normal user can query memories by type."""
        client.post(
            "/api/v2/memories",
            json={
                "content": "User prefers dark mode",
                "scope": "user",
                "memory_type": "preference",
                "importance": 0.5,
            },
            headers=normal_headers,
        )

        response = client.post(
            "/api/v2/memories/query",
            json={"memory_type": "preference", "limit": 10},
            headers=normal_headers,
        )
        assert response.status_code == 200

    def test_normal_user_delete_own_memory(self, client, normal_headers):
        """Normal user can delete their own memories."""
        store_resp = client.post(
            "/api/v2/memories",
            json={
                "content": "Temporary note by normal user",
                "scope": "user",
                "memory_type": "fact",
            },
            headers=normal_headers,
        )
        assert store_resp.status_code == 201
        memory_id = store_resp.json()["memory_id"]

        delete_resp = client.delete(
            f"/api/v2/memories/{memory_id}",
            headers=normal_headers,
        )
        assert delete_resp.status_code == 200
        assert delete_resp.json()["deleted"] is True


# ==============================================================================
# Tests: Revoked Key
# ==============================================================================


class TestRevokedKey:
    """Revoked API keys should be rejected."""

    def test_revoked_key_returns_401(self, client, api_keys, db_session_factory):
        """Revoked key -> 401 on store."""
        with db_session_factory() as session:
            DatabaseAPIKeyAuth.revoke_key(session, api_keys["normal_key_id"])
            session.commit()

        response = client.post(
            "/api/v2/memories",
            json={"content": "Should fail", "scope": "user"},
            headers={"Authorization": f"Bearer {api_keys['normal_key']}"},
        )
        assert response.status_code == 401


# ==============================================================================
# Tests: Paging with Auth
# ==============================================================================


class TestPagingWithAuth:
    """Memory paging behavior through authenticated HTTP endpoints."""

    def test_paging_distributes_across_tiers_admin(self, client, admin_headers):
        """Admin storing 15+ memories triggers paging across tiers."""
        memory_ids = []
        for i in range(15):
            response = client.post(
                "/api/v2/memories",
                json={
                    "content": f"Admin memory {i}: fact about topic {i % 3}",
                    "scope": "user",
                    "memory_type": "fact",
                    "importance": 0.5 + (i % 10) * 0.05,
                },
                headers=admin_headers,
            )
            assert response.status_code == 201
            memory_ids.append(response.json()["memory_id"])

        assert len(memory_ids) == 15

        stats_resp = client.get("/api/v2/memories/stats", headers=admin_headers)
        assert stats_resp.status_code == 200
        stats = stats_resp.json()

        # Paging must be enabled (configured in fixture)
        assert stats["paging_enabled"] is True
        # Main context capped at capacity (10)
        assert stats["main"]["count"] <= 10
        # All 15 memories accounted for across tiers
        assert stats["total_memories"] == 15
        # Some should have been evicted to recall
        assert stats["recall"]["count"] > 0

    def test_paging_distributes_across_tiers_normal(self, client, normal_headers):
        """Normal user storing 15+ memories triggers paging across tiers."""
        memory_ids = []
        for i in range(15):
            response = client.post(
                "/api/v2/memories",
                json={
                    "content": f"Normal memory {i}: note about topic {i % 4}",
                    "scope": "user",
                    "memory_type": "fact",
                    "importance": 0.4 + (i % 10) * 0.05,
                },
                headers=normal_headers,
            )
            assert response.status_code == 201
            memory_ids.append(response.json()["memory_id"])

        assert len(memory_ids) == 15

        stats_resp = client.get("/api/v2/memories/stats", headers=normal_headers)
        assert stats_resp.status_code == 200
        stats = stats_resp.json()

        # Paging must be enabled (configured in fixture)
        assert stats["paging_enabled"] is True
        # Main context capped at capacity (10)
        assert stats["main"]["count"] <= 10
        # All 15 memories accounted for across tiers
        assert stats["total_memories"] == 15
        # Some should have been evicted to recall
        assert stats["recall"]["count"] > 0
