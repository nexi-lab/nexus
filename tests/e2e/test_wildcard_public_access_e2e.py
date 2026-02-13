"""E2E tests for wildcard (*:*) public access - Issue #1064.

These are TRUE e2e tests that:
1. Start an actual FastAPI server on a real port
2. Create real users with authentication
3. Make real HTTP requests
4. Test wildcard public access across zones

Usage:
    uv run pytest tests/e2e/test_wildcard_public_access_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

# JWT secret must match conftest.py
JWT_SECRET = "test-secret-key-for-e2e-12345"


def create_test_user(db_path: Path, zone_id: str = "default") -> dict:
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
    zone_id: str = "default",
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
                 zone_id, subject_zone_id, object_zone_id)
                VALUES (:tuple_id, :subject_type, :subject_id, :relation, :object_type, :object_id,
                        :zone_id, :subject_zone_id, :object_zone_id)
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
    def db_engine(self, isolated_db):
        """Create database with ReBAC tables."""
        engine = create_engine(f"sqlite:///{isolated_db}")

        # Create tables
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS rebac_tuples (
                    tuple_id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    subject_relation TEXT,
                    relation TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    zone_id TEXT NOT NULL DEFAULT 'default',
                    conditions TEXT,
                    expires_at TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                )
            """)
            )

            # Create namespace config
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS rebac_namespaces (
                    namespace_id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    config TEXT NOT NULL
                )
            """)
            )

            # Insert file namespace with reader -> read permission
            config = json.dumps(
                {
                    "relations": {"reader": {}, "writer": {}, "owner": {}},
                    "permissions": {
                        "read": {"union": ["reader", "writer", "owner"]},
                        "write": {"union": ["writer", "owner"]},
                    },
                }
            )
            conn.execute(
                text(
                    "INSERT INTO rebac_namespaces (namespace_id, object_type, config) VALUES (:id, :type, :config)"
                ),
                {"id": "file_ns", "type": "file", "config": config},
            )

            # Create group closure table
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS rebac_group_closure (
                    member_type TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    group_type TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    zone_id TEXT NOT NULL DEFAULT 'default',
                    depth INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMP,
                    PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)
                )
            """)
            )
            conn.commit()

        yield engine
        engine.dispose()

    @pytest.fixture
    def async_manager(self, db_engine, isolated_db):
        """Create AsyncReBACManager for testing."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from nexus.services.permissions.async_rebac_manager import AsyncReBACManager

        async_engine = create_async_engine(f"sqlite+aiosqlite:///{isolated_db}")
        manager = AsyncReBACManager(async_engine, enable_l1_cache=False)
        yield manager

    @pytest.mark.asyncio
    async def test_wildcard_grants_access_cross_zone(self, async_manager, db_engine):
        """Test wildcard grants access to users from any zone."""
        # Write wildcard tuple directly to DB
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation, object_type, object_id, zone_id)
                    VALUES (:tuple_id, '*', '*', 'reader', 'file', '/public/doc.txt', 'owner-zone')
                """),
                {"tuple_id": str(uuid.uuid4())},
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
        # Write specific user tuple (not wildcard)
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation, object_type, object_id, zone_id)
                    VALUES (:tuple_id, 'user', 'specific-user', 'reader', 'file', '/private/doc.txt', 'owner-zone')
                """),
                {"tuple_id": str(uuid.uuid4())},
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
        # Write wildcard reader tuple
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation, object_type, object_id, zone_id)
                    VALUES (:tuple_id, '*', '*', 'reader', 'file', '/public/readonly.txt', 'default')
                """),
                {"tuple_id": str(uuid.uuid4())},
            )
            conn.commit()

        # Should have read
        result = await async_manager.rebac_check(
            subject=("user", "anyone"),
            permission="read",
            object=("file", "/public/readonly.txt"),
            zone_id="default",
        )
        assert result is True

        # Should NOT have write
        result = await async_manager.rebac_check(
            subject=("user", "anyone"),
            permission="write",
            object=("file", "/public/readonly.txt"),
            zone_id="default",
        )
        assert result is False, "Wildcard reader should not grant write permission"


class TestWildcardPerformance:
    """Performance tests to verify wildcard check doesn't impact performance."""

    @pytest.fixture
    def db_engine(self, isolated_db):
        """Create database with ReBAC tables and many tuples."""
        engine = create_engine(f"sqlite:///{isolated_db}")

        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS rebac_tuples (
                    tuple_id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    subject_relation TEXT,
                    relation TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    zone_id TEXT NOT NULL DEFAULT 'default',
                    conditions TEXT,
                    expires_at TEXT
                )
            """)
            )
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS rebac_namespaces (
                    namespace_id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    config TEXT NOT NULL
                )
            """)
            )
            config = json.dumps(
                {
                    "relations": {"reader": {}, "writer": {}},
                    "permissions": {"read": ["reader", "writer"], "write": ["writer"]},
                }
            )
            conn.execute(
                text(
                    "INSERT INTO rebac_namespaces (namespace_id, object_type, config) VALUES (:id, :type, :config)"
                ),
                {"id": "file_ns", "type": "file", "config": config},
            )
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS rebac_group_closure (
                    member_type TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    group_type TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    zone_id TEXT NOT NULL DEFAULT 'default',
                    depth INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)
                )
            """)
            )

            # Create index for performance
            conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_rebac_subject
                ON rebac_tuples (subject_type, subject_id, relation, object_type, object_id)
            """)
            )
            conn.commit()

        yield engine
        engine.dispose()

    @pytest.mark.asyncio
    async def test_wildcard_check_performance(self, db_engine, isolated_db):
        """Benchmark wildcard check to ensure no performance regression."""
        import time

        from sqlalchemy.ext.asyncio import create_async_engine

        from nexus.services.permissions.async_rebac_manager import AsyncReBACManager

        async_engine = create_async_engine(f"sqlite+aiosqlite:///{isolated_db}")
        manager = AsyncReBACManager(async_engine, enable_l1_cache=False)

        # Insert many tuples to simulate real workload
        with db_engine.connect() as conn:
            for i in range(1000):
                conn.execute(
                    text("""
                        INSERT INTO rebac_tuples
                        (tuple_id, subject_type, subject_id, relation, object_type, object_id, zone_id)
                        VALUES (:tuple_id, 'user', :user_id, 'reader', 'file', :file_path, 'default')
                    """),
                    {
                        "tuple_id": str(uuid.uuid4()),
                        "user_id": f"user-{i}",
                        "file_path": f"/files/doc-{i}.txt",
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
                zone_id="default",
            )
            # Should be False (no wildcard, no direct grant)
            assert result is False

        elapsed = time.perf_counter() - start
        avg_ms = (elapsed / iterations) * 1000

        print(f"\nPerformance: {iterations} permission checks in {elapsed:.3f}s")
        print(f"Average: {avg_ms:.3f}ms per check")

        # Assert reasonable performance (should be < 10ms per check even on SQLite)
        assert avg_ms < 50, f"Permission check too slow: {avg_ms:.3f}ms (expected < 50ms)"
