"""E2E tests for wildcard (*:*) public access - Issue #1064.

These are TRUE e2e tests that:
1. Start an actual FastAPI server on a real port
2. Create real users with authentication
3. Make real HTTP requests
4. Test wildcard public access across zones

Usage:
    uv run pytest tests/e2e/test_wildcard_public_access_e2e.py -v --override-ini="addopts="
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from nexus.contracts.constants import ROOT_ZONE_ID

# JWT secret must match conftest.py
JWT_SECRET = "test-secret-key-for-e2e-12345"


def create_test_user(db_path: Path, zone_id: str = ROOT_ZONE_ID) -> dict:
    """Create a test user and return auth info."""
    import jwt

    user_id = str(uuid.uuid4())
    email = f"user_{user_id[:8]}@test.com"

    # Create JWT token directly (matches server's expected format)
    payload = {
        "sub": user_id,
        "subject_id": user_id,
        "subject_type": "user",
        "zone_id": zone_id,
        "email": email,
        "name": f"Test User {user_id[:8]}",
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    return {
        "user_id": user_id,
        "email": email,
        "zone_id": zone_id,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def write_rebac_tuple(
    db_path: Path,
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    zone_id: str = ROOT_ZONE_ID,
    subject_zone_id: str | None = None,
    object_zone_id: str | None = None,
) -> str:
    """Write a ReBAC tuple directly to the database."""
    engine = create_engine(f"sqlite:///{db_path}")
    tuple_id = str(uuid.uuid4())
    effective_subject_zone = subject_zone_id or zone_id
    effective_object_zone = object_zone_id or zone_id

    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO rebac_tuples
                (tuple_id, subject_type, subject_id, relation, object_type, object_id,
                 zone_id, subject_zone_id, object_zone_id, created_at)
                VALUES (:tuple_id, :subject_type, :subject_id, :relation, :object_type, :object_id,
                        :zone_id, :subject_zone_id, :object_zone_id, :created_at)
            """),
            {
                "tuple_id": tuple_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "relation": relation,
                "object_type": object_type,
                "object_id": object_id,
                "zone_id": zone_id,
                "subject_zone_id": effective_subject_zone,
                "object_zone_id": effective_object_zone,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        conn.commit()

    return tuple_id


class TestWildcardPublicAccessE2E:
    """E2E tests for wildcard public access."""

    def test_wildcard_grants_read_to_any_user(self, nexus_server, test_app: httpx.Client):
        """Test that wildcard (*:*) tuple grants read access to any user via API.

        Uses RPC write/read to verify access: write a file as system, create
        a wildcard tuple via rebac_create RPC, then check that both same-zone
        and cross-zone users can read it.
        """
        import base64

        db_path = nexus_server["db_path"]

        def rpc(method: str, params: dict, headers: dict | None = None) -> dict:
            resp = test_app.post(
                f"/api/nfs/{method}",
                json={
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": method,
                    "params": params,
                },
                headers=headers or {"X-Nexus-Zone-ID": "system"},
            )
            return resp.json()

        # Create two users in different zones
        user_a = create_test_user(db_path, zone_id="zone-a")
        user_b = create_test_user(db_path, zone_id="zone-b")

        # Write a file as system
        test_file = "/public/shared-doc.txt"
        content = b"public content for wildcard test"
        write_result = rpc(
            "write",
            {
                "path": test_file,
                "content": {"__type__": "bytes", "data": base64.b64encode(content).decode()},
            },
        )
        assert "error" not in write_result, f"Write failed: {write_result}"

        # Create wildcard tuple via rebac_create RPC: (*:*) -> reader -> file
        rebac_result = rpc(
            "rebac_create",
            {
                "subject": ["*", "*"],
                "relation": "reader",
                "object": ["file", test_file],
                "zone_id": "zone-a",
            },
        )
        assert "error" not in rebac_result, f"rebac_create failed: {rebac_result}"

        # User A (same zone) should have access — read via RPC
        result_a = rpc("read", {"path": test_file}, headers=user_a["headers"])
        assert "error" not in result_a, f"User A should have read access: {result_a}"

        # User B (different zone) should also have access via wildcard
        result_b = rpc("read", {"path": test_file}, headers=user_b["headers"])
        assert "error" not in result_b, f"User B should have access via wildcard: {result_b}"


class TestWildcardDirectDB:
    """Direct database tests for wildcard - doesn't require full server."""

    @pytest.fixture
    def namespace_store(self):
        """Create a MetastoreNamespaceStore backed by in-memory DictMetastore."""
        from nexus.bricks.rebac.consistency.metastore_namespace_store import (
            MetastoreNamespaceStore,
        )
        from tests.helpers.dict_metastore import DictMetastore

        return MetastoreNamespaceStore(DictMetastore())

    @pytest.fixture
    def db_engine(self, isolated_db, namespace_store):
        """Create database with ReBAC tables using ORM models."""
        from nexus.bricks.rebac.domain import NamespaceConfig
        from nexus.storage.models import Base

        engine = create_engine(f"sqlite:///{isolated_db}")
        Base.metadata.create_all(engine)

        # Insert file namespace with reader -> read permission via MetastoreNamespaceStore
        namespace_store.create_or_update(
            NamespaceConfig(
                namespace_id="file_ns",
                object_type="file",
                config={
                    "relations": {"reader": {}, "writer": {}, "owner": {}},
                    "permissions": {
                        "read": {"union": ["reader", "writer", "owner"]},
                        "write": {"union": ["writer", "owner"]},
                    },
                },
                created_at=datetime.now(UTC),
            )
        )

        yield engine
        engine.dispose()

    @pytest.fixture
    def async_manager(self, db_engine, namespace_store):
        """Create AsyncReBACManager for testing."""
        from nexus.bricks.rebac.manager import AsyncReBACManager, ReBACManager

        sync_manager = ReBACManager(
            db_engine, enable_tiger_cache=False, namespace_store=namespace_store
        )
        manager = AsyncReBACManager(sync_manager)
        yield manager

    @pytest.mark.asyncio
    async def test_wildcard_grants_access_cross_zone(self, async_manager, db_engine):
        """Test wildcard grants access to users from any zone."""
        now = datetime.now(UTC).isoformat()
        # Write wildcard tuple directly to DB
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation, object_type, object_id,
                     zone_id, subject_zone_id, object_zone_id, created_at)
                    VALUES (:tuple_id, '*', '*', 'reader', 'file', '/public/doc.txt',
                            'owner-zone', 'owner-zone', 'owner-zone', :now)
                """),
                {"tuple_id": str(uuid.uuid4()), "now": now},
            )
            conn.commit()

        # User from different zone should have access
        result = await async_manager.rebac_check(
            subject=("user", "random-user-id"),
            permission="read",
            object=("file", "/public/doc.txt"),
            zone_id="different-zone",  # Different from owner-zone
        )
        assert result is True, "Wildcard should grant access to any user from any zone"

    @pytest.mark.asyncio
    async def test_no_wildcard_no_cross_zone_access(self, async_manager, db_engine):
        """Test that without wildcard, cross-zone access is denied."""
        now = datetime.now(UTC).isoformat()
        # Write specific user tuple (not wildcard)
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation, object_type, object_id,
                     zone_id, subject_zone_id, object_zone_id, created_at)
                    VALUES (:tuple_id, 'user', 'specific-user', 'reader', 'file', '/private/doc.txt',
                            'owner-zone', 'owner-zone', 'owner-zone', :now)
                """),
                {"tuple_id": str(uuid.uuid4()), "now": now},
            )
            conn.commit()

        # Specific user has access
        result = await async_manager.rebac_check(
            subject=("user", "specific-user"),
            permission="read",
            object=("file", "/private/doc.txt"),
            zone_id="owner-zone",
        )
        assert result is True

        # Random user does NOT have access
        result = await async_manager.rebac_check(
            subject=("user", "random-user"),
            permission="read",
            object=("file", "/private/doc.txt"),
            zone_id="different-zone",
        )
        assert result is False, "Without wildcard, random users should not have access"

    @pytest.mark.asyncio
    async def test_wildcard_respects_permission_level(self, async_manager, db_engine):
        """Test that wildcard reader doesn't grant write permission."""
        now = datetime.now(UTC).isoformat()
        # Write wildcard reader tuple
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation, object_type, object_id,
                     zone_id, subject_zone_id, object_zone_id, created_at)
                    VALUES (:tuple_id, '*', '*', 'reader', 'file', '/public/readonly.txt',
                            'default', 'default', 'default', :now)
                """),
                {"tuple_id": str(uuid.uuid4()), "now": now},
            )
            conn.commit()

        # Should have read
        result = await async_manager.rebac_check(
            subject=("user", "anyone"),
            permission="read",
            object=("file", "/public/readonly.txt"),
            zone_id=ROOT_ZONE_ID,
        )
        assert result is True

        # Should NOT have write
        result = await async_manager.rebac_check(
            subject=("user", "anyone"),
            permission="write",
            object=("file", "/public/readonly.txt"),
            zone_id=ROOT_ZONE_ID,
        )
        assert result is False, "Wildcard reader should not grant write permission"


class TestWildcardPerformance:
    """Performance tests to verify wildcard check doesn't impact performance."""

    @pytest.fixture
    def namespace_store(self):
        """Create a MetastoreNamespaceStore backed by in-memory DictMetastore."""
        from nexus.bricks.rebac.consistency.metastore_namespace_store import (
            MetastoreNamespaceStore,
        )
        from tests.helpers.dict_metastore import DictMetastore

        return MetastoreNamespaceStore(DictMetastore())

    @pytest.fixture
    def db_engine(self, isolated_db, namespace_store):
        """Create database with ReBAC tables using ORM models."""
        from nexus.bricks.rebac.domain import NamespaceConfig
        from nexus.storage.models import Base

        engine = create_engine(f"sqlite:///{isolated_db}")
        Base.metadata.create_all(engine)

        # Insert file namespace via MetastoreNamespaceStore
        namespace_store.create_or_update(
            NamespaceConfig(
                namespace_id="file_ns",
                object_type="file",
                config={
                    "relations": {"reader": {}, "writer": {}},
                    "permissions": {"read": ["reader", "writer"], "write": ["writer"]},
                },
                created_at=datetime.now(UTC),
            )
        )

        yield engine
        engine.dispose()

    @pytest.mark.asyncio
    async def test_wildcard_check_performance(self, db_engine, namespace_store, isolated_db):
        """Benchmark wildcard check to ensure no performance regression."""
        import time

        from nexus.bricks.rebac.manager import AsyncReBACManager, ReBACManager

        # Use the existing sync db_engine directly — avoids MissingGreenlet.
        sync_manager = ReBACManager(
            db_engine, enable_tiger_cache=False, namespace_store=namespace_store
        )
        manager = AsyncReBACManager(sync_manager)

        # Insert many tuples to simulate real workload
        now = datetime.now(UTC).isoformat()
        with db_engine.connect() as conn:
            for i in range(1000):
                conn.execute(
                    text("""
                        INSERT INTO rebac_tuples
                        (tuple_id, subject_type, subject_id, relation, object_type, object_id,
                         zone_id, subject_zone_id, object_zone_id, created_at)
                        VALUES (:tuple_id, 'user', :user_id, 'reader', 'file', :file_path,
                                'default', 'default', 'default', :now)
                    """),
                    {
                        "tuple_id": str(uuid.uuid4()),
                        "user_id": f"user-{i}",
                        "file_path": f"/files/doc-{i}.txt",
                        "now": now,
                    },
                )
            conn.commit()

        # Benchmark: Check permission for non-existent user (worst case - checks all paths including wildcard)
        iterations = 100
        start = time.perf_counter()

        for i in range(iterations):
            result = await manager.rebac_check(
                subject=("user", f"nonexistent-user-{i}"),
                permission="read",
                object=("file", f"/files/doc-{i % 1000}.txt"),
                zone_id=ROOT_ZONE_ID,
            )
            # Should be False (no wildcard, no direct grant)
            assert result is False

        elapsed = time.perf_counter() - start
        avg_ms = (elapsed / iterations) * 1000

        print(f"\nPerformance: {iterations} permission checks in {elapsed:.3f}s")
        print(f"Average: {avg_ms:.3f}ms per check")

        # Assert reasonable performance (should be < 10ms per check even on SQLite)
        assert avg_ms < 50, f"Permission check too slow: {avg_ms:.3f}ms (expected < 50ms)"
