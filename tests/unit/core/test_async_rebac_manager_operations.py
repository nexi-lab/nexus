"""Comprehensive async tests for AsyncReBACManager operations.

These tests verify that the async ReBAC implementation behaves identically
to the sync ReBACManager. They serve as the behavioral contract to ensure
async migration maintains correctness.

Tests cover:
- write_tuple: Create relationship tuples asynchronously
- delete_tuple: Delete tuples asynchronously
- rebac_check: Async permission checking with graph traversal
- rebac_check_bulk: Bulk async permission checks
- L1 cache behavior
- Cross-tenant isolation
- Permission hierarchy

Note: Uses `direct_owner` relation which grants `read` permission in the
default ReBAC namespace configuration.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.core.async_rebac_manager import AsyncReBACManager


@pytest_asyncio.fixture
async def temp_db() -> AsyncGenerator[Path, None]:
    """Create a temporary database path for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest_asyncio.fixture
async def engine(temp_db: Path) -> AsyncGenerator[AsyncEngine, None]:
    """Create async SQLAlchemy engine with initialized tables for testing."""
    # Use aiosqlite for async SQLite
    db_url = f"sqlite+aiosqlite:///{temp_db}"
    engine = create_async_engine(db_url, echo=False)

    # Initialize tables in engine fixture so all tests have access
    async with engine.begin() as conn:
        # Create rebac_tuples table
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS rebac_tuples (
                tuple_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_relation TEXT,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                conditions TEXT,
                expires_at TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        )

        # Create rebac_namespaces table with default namespace
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS rebac_namespaces (
                namespace_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                config TEXT NOT NULL
            )
        """)
        )

        # Insert default namespace config that maps direct_owner -> read
        import json

        config = json.dumps(
            {
                "relations": {"direct_owner": {}, "reader": {}, "writer": {}, "owner": {}},
                "permissions": {
                    "read": {"union": ["direct_owner", "reader", "writer", "owner"]},
                    "write": {"union": ["writer", "owner"]},
                    "admin": {"union": ["owner"]},
                },
            }
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO rebac_namespaces (namespace_id, object_type, config) VALUES (:id, :type, :config)"
            ),
            {"id": "file_ns", "type": "file", "config": config},
        )

        # Create group closure table for LEOPARD
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS rebac_group_closure (
                member_type TEXT NOT NULL,
                member_id TEXT NOT NULL,
                group_type TEXT NOT NULL,
                group_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                depth INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP,
                PRIMARY KEY (member_type, member_id, group_type, group_id, tenant_id)
            )
        """)
        )

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def manager(engine: AsyncEngine) -> AsyncGenerator[AsyncReBACManager, None]:
    """Create AsyncReBACManager with L1 cache enabled."""
    manager = AsyncReBACManager(engine, enable_l1_cache=True)
    yield manager


class TestAsyncWriteTuple:
    """Tests for async write_tuple method."""

    @pytest.mark.asyncio
    async def test_write_basic_tuple(self, manager: AsyncReBACManager) -> None:
        """Test creating a basic relationship tuple asynchronously."""
        tuple_id = await manager.write_tuple(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            tenant_id="default",
        )
        assert tuple_id is not None
        assert isinstance(tuple_id, str)
        assert len(tuple_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_write_with_different_tenant(self, manager: AsyncReBACManager) -> None:
        """Test creating tuple with specific tenant_id."""
        tuple_id = await manager.write_tuple(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            tenant_id="acme",
        )
        assert tuple_id is not None

    @pytest.mark.asyncio
    async def test_write_with_expiration(self, manager: AsyncReBACManager) -> None:
        """Test creating tuple with TTL expiration."""
        future_time = datetime.now(UTC) + timedelta(hours=1)
        tuple_id = await manager.write_tuple(
            subject=("user", "charlie"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            tenant_id="default",
            expires_at=future_time,
        )
        assert tuple_id is not None

    @pytest.mark.asyncio
    async def test_write_with_conditions(self, manager: AsyncReBACManager) -> None:
        """Test creating tuple with ABAC conditions."""
        tuple_id = await manager.write_tuple(
            subject=("user", "dave"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            tenant_id="default",
            conditions={"ip_range": "10.0.0.0/8"},
        )
        assert tuple_id is not None

    @pytest.mark.asyncio
    async def test_write_multiple_tuples(self, manager: AsyncReBACManager) -> None:
        """Test creating multiple tuples sequentially."""
        tuple_ids = []
        for i in range(5):
            tid = await manager.write_tuple(
                subject=("user", f"user_{i}"),
                relation="direct_owner",
                object=("file", f"/file_{i}.txt"),
                tenant_id="default",
            )
            tuple_ids.append(tid)
        assert len(tuple_ids) == 5
        assert len(set(tuple_ids)) == 5  # All unique


class TestAsyncDeleteTuple:
    """Tests for async delete_tuple method."""

    @pytest.mark.asyncio
    async def test_delete_existing_tuple(self, manager: AsyncReBACManager) -> None:
        """Test deleting an existing tuple."""
        # Create tuple
        await manager.write_tuple(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/to_delete.txt"),
            tenant_id="default",
        )

        # Verify it exists via check
        assert await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/to_delete.txt"),
            tenant_id="default",
        )

        # Delete
        deleted = await manager.delete_tuple(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/to_delete.txt"),
            tenant_id="default",
        )
        assert deleted is True

        # Verify it's gone
        assert not await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/to_delete.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_tuple(self, manager: AsyncReBACManager) -> None:
        """Test deleting a tuple that doesn't exist."""
        deleted = await manager.delete_tuple(
            subject=("user", "nobody"),
            relation="direct_owner",
            object=("file", "/nonexistent.txt"),
            tenant_id="default",
        )
        assert deleted is False


class TestAsyncRebacCheck:
    """Tests for async rebac_check method."""

    @pytest.mark.asyncio
    async def test_check_direct_permission(self, manager: AsyncReBACManager) -> None:
        """Test checking direct permission via relation."""
        # Create tuple
        await manager.write_tuple(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/check_doc.txt"),
            tenant_id="default",
        )

        # Check permission
        assert await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/check_doc.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_check_no_permission(self, manager: AsyncReBACManager) -> None:
        """Test checking permission when not granted."""
        result = await manager.rebac_check(
            subject=("user", "stranger"),
            permission="read",
            object=("file", "/secret.txt"),
            tenant_id="default",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_check_different_users(self, manager: AsyncReBACManager) -> None:
        """Test that permissions are user-specific."""
        # Grant to alice
        await manager.write_tuple(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/alice_doc.txt"),
            tenant_id="default",
        )

        # Alice has access
        assert await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/alice_doc.txt"),
            tenant_id="default",
        )

        # Bob doesn't
        assert not await manager.rebac_check(
            subject=("user", "bob"),
            permission="read",
            object=("file", "/alice_doc.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_check_expired_tuple(self, manager: AsyncReBACManager) -> None:
        """Test that expired tuples don't grant permission."""
        # Create tuple that already expired
        past_time = datetime.now(UTC) - timedelta(hours=1)
        await manager.write_tuple(
            subject=("user", "expired_user"),
            relation="direct_owner",
            object=("file", "/expired.txt"),
            tenant_id="default",
            expires_at=past_time,
        )

        # Should not have permission
        result = await manager.rebac_check(
            subject=("user", "expired_user"),
            permission="read",
            object=("file", "/expired.txt"),
            tenant_id="default",
        )
        assert result is False


class TestAsyncCrossTenant:
    """Tests for cross-tenant isolation."""

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, manager: AsyncReBACManager) -> None:
        """Test that permissions are isolated by tenant."""
        # Grant in tenant_a
        await manager.write_tuple(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/shared.txt"),
            tenant_id="tenant_a",
        )

        # Has access in tenant_a
        assert await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/shared.txt"),
            tenant_id="tenant_a",
        )

        # No access in tenant_b
        assert not await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/shared.txt"),
            tenant_id="tenant_b",
        )

    @pytest.mark.asyncio
    async def test_separate_tenant_tuples(self, manager: AsyncReBACManager) -> None:
        """Test that tuples in different tenants are independent."""
        # Same user, same object, different tenants
        await manager.write_tuple(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            tenant_id="acme",
        )
        await manager.write_tuple(
            subject=("user", "bob"),
            relation="reader",
            object=("file", "/doc.txt"),
            tenant_id="corp",
        )

        # Both should have read access in their respective tenants
        assert await manager.rebac_check(
            subject=("user", "bob"),
            permission="read",
            object=("file", "/doc.txt"),
            tenant_id="acme",
        )
        assert await manager.rebac_check(
            subject=("user", "bob"),
            permission="read",
            object=("file", "/doc.txt"),
            tenant_id="corp",
        )


class TestAsyncRebacCheckBulk:
    """Tests for async rebac_check_bulk method."""

    @pytest.mark.asyncio
    async def test_bulk_check_all_allowed(self, manager: AsyncReBACManager) -> None:
        """Test bulk check where all permissions are granted."""
        # Create tuples
        for i in range(3):
            await manager.write_tuple(
                subject=("user", f"bulk_user_{i}"),
                relation="direct_owner",
                object=("file", f"/bulk_file_{i}.txt"),
                tenant_id="default",
            )

        # Bulk check - returns dict mapping check -> result
        checks = [
            (("user", f"bulk_user_{i}"), "read", ("file", f"/bulk_file_{i}.txt")) for i in range(3)
        ]
        results = await manager.rebac_check_bulk(checks, tenant_id="default")

        assert len(results) == 3
        assert all(v is True for v in results.values())

    @pytest.mark.asyncio
    async def test_bulk_check_mixed_results(self, manager: AsyncReBACManager) -> None:
        """Test bulk check with mixed allowed/denied results."""
        # Only grant access to first file
        await manager.write_tuple(
            subject=("user", "mixed_user"),
            relation="direct_owner",
            object=("file", "/allowed.txt"),
            tenant_id="default",
        )

        check_allowed = (("user", "mixed_user"), "read", ("file", "/allowed.txt"))
        check_denied = (("user", "mixed_user"), "read", ("file", "/denied.txt"))
        checks = [check_allowed, check_denied]
        results = await manager.rebac_check_bulk(checks, tenant_id="default")

        assert results[check_allowed] is True
        assert results[check_denied] is False

    @pytest.mark.asyncio
    async def test_bulk_check_empty_list(self, manager: AsyncReBACManager) -> None:
        """Test bulk check with empty list."""
        results = await manager.rebac_check_bulk([], tenant_id="default")
        assert results == {}  # Returns empty dict, not list


class TestAsyncCacheBehavior:
    """Tests for async L1 cache behavior."""

    @pytest.mark.asyncio
    async def test_cache_hit_on_repeated_check(self, manager: AsyncReBACManager) -> None:
        """Test that repeated checks use cache."""
        # Create tuple
        await manager.write_tuple(
            subject=("user", "cache_user"),
            relation="direct_owner",
            object=("file", "/cache_test.txt"),
            tenant_id="default",
        )

        # First check (cache miss)
        result1 = await manager.rebac_check(
            subject=("user", "cache_user"),
            permission="read",
            object=("file", "/cache_test.txt"),
            tenant_id="default",
        )

        # Second check (cache hit)
        result2 = await manager.rebac_check(
            subject=("user", "cache_user"),
            permission="read",
            object=("file", "/cache_test.txt"),
            tenant_id="default",
        )

        assert result1 is True
        assert result2 is True

        # Verify cache stats
        if manager._l1_cache:
            stats = manager.get_l1_cache_stats()
            assert stats.get("hits", 0) > 0

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_write(self, manager: AsyncReBACManager) -> None:
        """Test that cache is invalidated when tuples are written."""
        # Initially no access
        assert not await manager.rebac_check(
            subject=("user", "inv_user"),
            permission="read",
            object=("file", "/inv_doc.txt"),
            tenant_id="default",
        )

        # Write tuple
        await manager.write_tuple(
            subject=("user", "inv_user"),
            relation="direct_owner",
            object=("file", "/inv_doc.txt"),
            tenant_id="default",
        )

        # Now should have access (cache invalidated)
        assert await manager.rebac_check(
            subject=("user", "inv_user"),
            permission="read",
            object=("file", "/inv_doc.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_delete(self, manager: AsyncReBACManager) -> None:
        """Test that cache is invalidated when tuples are deleted."""
        # Create tuple
        await manager.write_tuple(
            subject=("user", "del_user"),
            relation="direct_owner",
            object=("file", "/del_doc.txt"),
            tenant_id="default",
        )

        # Should have access
        assert await manager.rebac_check(
            subject=("user", "del_user"),
            permission="read",
            object=("file", "/del_doc.txt"),
            tenant_id="default",
        )

        # Delete tuple
        await manager.delete_tuple(
            subject=("user", "del_user"),
            relation="direct_owner",
            object=("file", "/del_doc.txt"),
            tenant_id="default",
        )

        # Should no longer have access (cache invalidated)
        assert not await manager.rebac_check(
            subject=("user", "del_user"),
            permission="read",
            object=("file", "/del_doc.txt"),
            tenant_id="default",
        )


class TestAsyncConcurrency:
    """Tests for concurrent async operations."""

    @pytest.mark.asyncio
    async def test_concurrent_writes(self, manager: AsyncReBACManager) -> None:
        """Test concurrent tuple writes."""

        async def write_tuple(i: int) -> str:
            return await manager.write_tuple(
                subject=("user", f"conc_user_{i}"),
                relation="direct_owner",
                object=("file", f"/conc_file_{i}.txt"),
                tenant_id="default",
            )

        # Write 10 tuples concurrently
        tasks = [write_tuple(i) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        assert len(set(results)) == 10  # All unique tuple IDs

    @pytest.mark.asyncio
    async def test_concurrent_checks(self, manager: AsyncReBACManager) -> None:
        """Test concurrent permission checks."""
        # Pre-create tuples
        for i in range(5):
            await manager.write_tuple(
                subject=("user", f"check_user_{i}"),
                relation="direct_owner",
                object=("file", f"/check_file_{i}.txt"),
                tenant_id="default",
            )

        async def check_permission(i: int) -> bool:
            return await manager.rebac_check(
                subject=("user", f"check_user_{i}"),
                permission="read",
                object=("file", f"/check_file_{i}.txt"),
                tenant_id="default",
            )

        # Check 5 permissions concurrently
        tasks = [check_permission(i) for i in range(5)]
        results = await asyncio.gather(*tasks)

        assert all(r is True for r in results)


class TestAsyncPermissionHierarchy:
    """Tests for permission hierarchy resolution."""

    @pytest.mark.asyncio
    async def test_reader_has_read_permission(self, manager: AsyncReBACManager) -> None:
        """Test that reader relation grants read permission."""
        await manager.write_tuple(
            subject=("user", "reader"),
            relation="reader",
            object=("file", "/readable.txt"),
            tenant_id="default",
        )

        assert await manager.rebac_check(
            subject=("user", "reader"),
            permission="read",
            object=("file", "/readable.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_writer_has_read_and_write(self, manager: AsyncReBACManager) -> None:
        """Test that writer relation grants read and write permissions."""
        await manager.write_tuple(
            subject=("user", "writer"),
            relation="writer",
            object=("file", "/writable.txt"),
            tenant_id="default",
        )

        # Has read
        assert await manager.rebac_check(
            subject=("user", "writer"),
            permission="read",
            object=("file", "/writable.txt"),
            tenant_id="default",
        )

        # Has write
        assert await manager.rebac_check(
            subject=("user", "writer"),
            permission="write",
            object=("file", "/writable.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_owner_has_all_permissions(self, manager: AsyncReBACManager) -> None:
        """Test that owner relation grants all permissions."""
        await manager.write_tuple(
            subject=("user", "owner"),
            relation="owner",
            object=("file", "/owned.txt"),
            tenant_id="default",
        )

        # Has read
        assert await manager.rebac_check(
            subject=("user", "owner"),
            permission="read",
            object=("file", "/owned.txt"),
            tenant_id="default",
        )

        # Has write
        assert await manager.rebac_check(
            subject=("user", "owner"),
            permission="write",
            object=("file", "/owned.txt"),
            tenant_id="default",
        )

        # Has admin
        assert await manager.rebac_check(
            subject=("user", "owner"),
            permission="admin",
            object=("file", "/owned.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_reader_no_write_permission(self, manager: AsyncReBACManager) -> None:
        """Test that reader relation does not grant write permission."""
        await manager.write_tuple(
            subject=("user", "readonly"),
            relation="reader",
            object=("file", "/readonly.txt"),
            tenant_id="default",
        )

        # Has read
        assert await manager.rebac_check(
            subject=("user", "readonly"),
            permission="read",
            object=("file", "/readonly.txt"),
            tenant_id="default",
        )

        # No write
        assert not await manager.rebac_check(
            subject=("user", "readonly"),
            permission="write",
            object=("file", "/readonly.txt"),
            tenant_id="default",
        )


class TestAsyncNamespaceConfig:
    """Tests for namespace configuration loading."""

    @pytest.mark.asyncio
    async def test_namespace_loads_on_first_check(self, manager: AsyncReBACManager) -> None:
        """Test that namespaces are loaded on first permission check."""
        # Initially not loaded
        assert not manager._namespaces_loaded

        # Do a check
        await manager.rebac_check(
            subject=("user", "test"),
            permission="read",
            object=("file", "/test.txt"),
            tenant_id="default",
        )

        # Now should be loaded
        assert manager._namespaces_loaded
        assert "file" in manager._namespaces

    @pytest.mark.asyncio
    async def test_get_namespace(self, manager: AsyncReBACManager) -> None:
        """Test getting namespace configuration."""
        # Trigger loading
        await manager.rebac_check(
            subject=("user", "test"),
            permission="read",
            object=("file", "/test.txt"),
            tenant_id="default",
        )

        # Get namespace
        ns = manager.get_namespace("file")
        assert ns is not None
        assert ns.object_type == "file"

    @pytest.mark.asyncio
    async def test_get_nonexistent_namespace(self, manager: AsyncReBACManager) -> None:
        """Test getting namespace that doesn't exist."""
        # Trigger loading
        await manager.rebac_check(
            subject=("user", "test"),
            permission="read",
            object=("file", "/test.txt"),
            tenant_id="default",
        )

        # Get nonexistent
        ns = manager.get_namespace("nonexistent")
        assert ns is None


class TestAsyncManagerWithoutCache:
    """Tests for AsyncReBACManager without L1 cache."""

    @pytest.mark.asyncio
    async def test_check_without_cache(self, engine: AsyncEngine) -> None:
        """Test permission check without L1 cache."""
        # Create manager without cache, using same engine (which has tables)
        manager_no_cache = AsyncReBACManager(engine, enable_l1_cache=False)

        await manager_no_cache.write_tuple(
            subject=("user", "nocache"),
            relation="direct_owner",
            object=("file", "/nocache.txt"),
            tenant_id="default",
        )

        result = await manager_no_cache.rebac_check(
            subject=("user", "nocache"),
            permission="read",
            object=("file", "/nocache.txt"),
            tenant_id="default",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_cache_stats_empty_without_cache(self, engine: AsyncEngine) -> None:
        """Test that cache stats are empty when cache is disabled."""
        manager_no_cache = AsyncReBACManager(engine, enable_l1_cache=False)
        stats = manager_no_cache.get_l1_cache_stats()
        assert stats == {}


class TestWildcardPublicAccess:
    """Tests for wildcard (*:*) public access - Issue #1064.

    Verifies that wildcard subjects grant access to ALL users regardless of tenant.
    This is the industry-standard pattern used by SpiceDB, OpenFGA, and Ory Keto.
    """

    @pytest.mark.asyncio
    async def test_wildcard_grants_access_to_any_user(self, manager: AsyncReBACManager) -> None:
        """Test that wildcard (*:*) tuple grants access to any user."""
        # Create wildcard public access tuple
        await manager.write_tuple(
            subject=("*", "*"),  # Wildcard subject
            relation="reader",
            object=("file", "/public/doc.txt"),
            tenant_id="default",
        )

        # Any user should have access
        assert await manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/public/doc.txt"),
            tenant_id="default",
        )

        assert await manager.rebac_check(
            subject=("user", "bob"),
            permission="read",
            object=("file", "/public/doc.txt"),
            tenant_id="default",
        )

        assert await manager.rebac_check(
            subject=("agent", "some-agent"),
            permission="read",
            object=("file", "/public/doc.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_wildcard_cross_tenant_access(self, manager: AsyncReBACManager) -> None:
        """Test that wildcard grants access across different tenants."""
        # Create wildcard tuple in tenant A
        await manager.write_tuple(
            subject=("*", "*"),
            relation="reader",
            object=("file", "/shared/public.txt"),
            tenant_id="tenant-a",
        )

        # User in tenant B should have access (cross-tenant via wildcard)
        assert await manager.rebac_check(
            subject=("user", "user-from-tenant-b"),
            permission="read",
            object=("file", "/shared/public.txt"),
            tenant_id="tenant-b",
        )

    @pytest.mark.asyncio
    async def test_wildcard_does_not_grant_higher_permissions(
        self, manager: AsyncReBACManager
    ) -> None:
        """Test that wildcard reader does not grant write permission."""
        # Create wildcard reader access
        await manager.write_tuple(
            subject=("*", "*"),
            relation="reader",
            object=("file", "/public/readonly.txt"),
            tenant_id="default",
        )

        # Should have read
        assert await manager.rebac_check(
            subject=("user", "random"),
            permission="read",
            object=("file", "/public/readonly.txt"),
            tenant_id="default",
        )

        # Should NOT have write
        assert not await manager.rebac_check(
            subject=("user", "random"),
            permission="write",
            object=("file", "/public/readonly.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_no_wildcard_means_no_public_access(
        self, manager: AsyncReBACManager
    ) -> None:
        """Test that without wildcard, random users don't have access."""
        # Create specific user access (not wildcard)
        await manager.write_tuple(
            subject=("user", "specific-user"),
            relation="reader",
            object=("file", "/private/doc.txt"),
            tenant_id="default",
        )

        # Specific user has access
        assert await manager.rebac_check(
            subject=("user", "specific-user"),
            permission="read",
            object=("file", "/private/doc.txt"),
            tenant_id="default",
        )

        # Random user does NOT have access
        assert not await manager.rebac_check(
            subject=("user", "random-user"),
            permission="read",
            object=("file", "/private/doc.txt"),
            tenant_id="default",
        )

    @pytest.mark.asyncio
    async def test_wildcard_with_specific_user_both_work(
        self, manager: AsyncReBACManager
    ) -> None:
        """Test that both wildcard and specific user grants work together."""
        # Create wildcard reader access
        await manager.write_tuple(
            subject=("*", "*"),
            relation="reader",
            object=("file", "/mixed/doc.txt"),
            tenant_id="default",
        )

        # Create specific user writer access
        await manager.write_tuple(
            subject=("user", "editor"),
            relation="writer",
            object=("file", "/mixed/doc.txt"),
            tenant_id="default",
        )

        # Random user has read (via wildcard)
        assert await manager.rebac_check(
            subject=("user", "random"),
            permission="read",
            object=("file", "/mixed/doc.txt"),
            tenant_id="default",
        )

        # Random user does NOT have write
        assert not await manager.rebac_check(
            subject=("user", "random"),
            permission="write",
            object=("file", "/mixed/doc.txt"),
            tenant_id="default",
        )

        # Editor has both read and write
        assert await manager.rebac_check(
            subject=("user", "editor"),
            permission="read",
            object=("file", "/mixed/doc.txt"),
            tenant_id="default",
        )
        assert await manager.rebac_check(
            subject=("user", "editor"),
            permission="write",
            object=("file", "/mixed/doc.txt"),
            tenant_id="default",
        )
