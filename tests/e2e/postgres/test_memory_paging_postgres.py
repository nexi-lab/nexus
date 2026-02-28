"""Integration tests for memory paging with PostgreSQL + permissions + database auth.

Tests the production-equivalent configuration:
- PostgreSQL (pgvector/pg17) instead of SQLite
- enforce_permissions=True (ReBAC permission checks on every query)
- DiscriminatingAuthProvider wrapping DatabaseAPIKeyAuth (nexus serve --auth-type database)
- Memory paging enabled (3-tier: main context -> recall -> archival)

Requirements:
    docker compose --profile test up postgres-test -d
    postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test

Run:
    pytest tests/integration/test_memory_paging_postgres.py -v
"""

import shutil
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

POSTGRES_URL = "postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test"


@pytest.fixture(scope="module")
def pg_engine():
    """Create PostgreSQL engine; skip if unavailable."""
    try:
        engine = create_engine(POSTGRES_URL, echo=False)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL not available at {POSTGRES_URL}: {e}")

    from nexus.storage.models import Base

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    """Session factory bound to test PostgreSQL."""
    return sessionmaker(bind=pg_engine)


@pytest.fixture
def pg_session(pg_session_factory):
    """Session for direct DB setup in tests."""
    sess = pg_session_factory()
    yield sess
    sess.close()


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Required env vars for server modules."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-postgres-paging")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)


@pytest.fixture
def api_keys(pg_session_factory):
    """Create admin + normal API keys via DatabaseAPIKeyAuth."""
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

    with pg_session_factory() as session:
        admin_key_id, admin_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="pg-admin",
            name="PG Admin Key",
            zone_id="root",
            is_admin=True,
        )
        normal_key_id, normal_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="pg-normal",
            name="PG Normal Key",
            zone_id="root",
            is_admin=False,
        )
        session.commit()

    yield {
        "admin_key": admin_raw,
        "admin_key_id": admin_key_id,
        "normal_key": normal_raw,
        "normal_key_id": normal_key_id,
    }

    # Cleanup keys
    with pg_session_factory() as session:
        session.execute(
            text("DELETE FROM api_keys WHERE key_id IN (:a, :b)"),
            {"a": admin_key_id, "b": normal_key_id},
        )
        session.commit()


@pytest.fixture
def app(tmp_path, pg_engine, pg_session_factory, api_keys):
    """FastAPI app with PostgreSQL, permissions enabled, database auth."""
    from nexus.backends.local import LocalBackend
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.bricks.auth.providers.discriminator import DiscriminatingAuthProvider
    from nexus.core.config import MemoryConfig, PermissionConfig
    from nexus.core.nexus_fs import NexusFS
    from nexus.server.fastapi_server import create_app

    tmpdir = tempfile.mkdtemp(prefix="nexus-pg-paging-")
    backend = LocalBackend(root_path=tmpdir)

    from nexus.storage.record_store import SQLAlchemyRecordStore
    from tests.helpers.dict_metastore import DictMetastore

    record_db_path = tmp_path / "pg_records.db"
    record_store = SQLAlchemyRecordStore(db_path=str(record_db_path))
    metadata_store = DictMetastore()

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=True),  # Production: permissions ON
        memory=MemoryConfig(enable_paging=True, main_capacity=10),
    )

    # Production wiring: DiscriminatingAuthProvider wrapping DatabaseAPIKeyAuth
    from types import SimpleNamespace

    db_key_auth = DatabaseAPIKeyAuth(
        record_store=SimpleNamespace(session_factory=pg_session_factory)
    )
    auth_provider = DiscriminatingAuthProvider(
        api_key_provider=db_key_auth,
        jwt_provider=None,
    )

    application = create_app(
        nexus_fs=nx,
        auth_provider=auth_provider,
        database_url=POSTGRES_URL,
    )

    yield application

    metadata_store.close()
    record_store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    # Cleanup memories created during test
    with pg_session_factory() as session:
        session.execute(
            text(
                f"DELETE FROM memories WHERE zone_id = '{ROOT_ZONE_ID}' AND user_id IN ('pg-admin', 'pg-normal')"
            )
        )
        session.commit()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def admin_headers(api_keys):
    return {"Authorization": f"Bearer {api_keys['admin_key']}"}


@pytest.fixture
def normal_headers(api_keys):
    return {"Authorization": f"Bearer {api_keys['normal_key']}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPostgresPermissionsPaging:
    """Full production-equivalent: PostgreSQL + permissions + database auth + paging."""

    def test_health_shows_permissions_enabled(self, client):
        """Health endpoint should report enforce_permissions=True."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enforce_permissions"] is True
        assert data["has_auth"] is True

    def test_unauthenticated_rejected(self, client):
        """No token -> 401."""
        resp = client.post(
            "/api/v2/memories",
            json={"content": "Should fail", "scope": "user"},
        )
        assert resp.status_code == 401

    def test_admin_store_and_query(self, client, admin_headers):
        """Admin can store + query with permissions enabled."""
        # Store
        store_resp = client.post(
            "/api/v2/memories",
            json={
                "content": "PostgreSQL integration fact",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.8,
            },
            headers=admin_headers,
        )
        assert store_resp.status_code == 201
        memory_id = store_resp.json()["memory_id"]
        assert memory_id

        # Query
        query_resp = client.post(
            "/api/v2/memories/query",
            json={"memory_type": "fact", "limit": 10},
            headers=admin_headers,
        )
        assert query_resp.status_code == 200
        data = query_resp.json()
        assert data["total"] >= 1

    def test_normal_user_store_and_query(self, client, normal_headers):
        """Normal user can store + query their own memories with permissions."""
        store_resp = client.post(
            "/api/v2/memories",
            json={
                "content": "Normal user PostgreSQL fact",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.6,
            },
            headers=normal_headers,
        )
        assert store_resp.status_code == 201
        memory_id = store_resp.json()["memory_id"]
        assert memory_id

        query_resp = client.post(
            "/api/v2/memories/query",
            json={"memory_type": "fact", "limit": 10},
            headers=normal_headers,
        )
        assert query_resp.status_code == 200
        data = query_resp.json()
        assert data["total"] >= 1

    def test_paging_distribution_with_permissions(self, client, admin_headers):
        """Paging distributes memories across tiers with permissions enabled."""
        # Store 15 memories (capacity = 10)
        for i in range(15):
            resp = client.post(
                "/api/v2/memories",
                json={
                    "content": f"PG paging test memory {i}",
                    "scope": "user",
                    "memory_type": "fact",
                    "importance": 0.5 + (i % 10) * 0.05,
                },
                headers=admin_headers,
            )
            assert resp.status_code == 201, f"Store {i} failed: {resp.text}"

        # Check stats
        stats_resp = client.get("/api/v2/memories/stats", headers=admin_headers)
        assert stats_resp.status_code == 200
        stats = stats_resp.json()

        assert stats["paging_enabled"] is True
        assert stats["main"]["count"] <= 10
        assert stats["total_memories"] == 15
        assert stats["recall"]["count"] > 0

    def test_search_with_permissions(self, client, admin_headers):
        """Search works through FastAPI with permissions on PostgreSQL."""
        # Store a searchable memory
        client.post(
            "/api/v2/memories",
            json={
                "content": "The Eiffel Tower is 330 metres tall",
                "scope": "user",
                "memory_type": "fact",
                "importance": 0.9,
            },
            headers=admin_headers,
        )

        # Search
        resp = client.post(
            "/api/v2/memories/search",
            json={"query": "Eiffel Tower", "limit": 5, "search_mode": "keyword"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    def test_delete_with_permissions(self, client, admin_headers):
        """Delete works with permissions enabled."""
        store_resp = client.post(
            "/api/v2/memories",
            json={"content": "Temporary PG fact", "scope": "user", "memory_type": "fact"},
            headers=admin_headers,
        )
        assert store_resp.status_code == 201
        memory_id = store_resp.json()["memory_id"]

        del_resp = client.delete(f"/api/v2/memories/{memory_id}", headers=admin_headers)
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

    def test_revoked_key_rejected(self, client, api_keys, pg_session_factory):
        """Revoked key -> 401 on PostgreSQL."""
        from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

        with pg_session_factory() as session:
            DatabaseAPIKeyAuth.revoke_key(session, api_keys["normal_key_id"])
            session.commit()

        resp = client.post(
            "/api/v2/memories",
            json={"content": "Should fail", "scope": "user"},
            headers={"Authorization": f"Bearer {api_keys['normal_key']}"},
        )
        assert resp.status_code == 401
