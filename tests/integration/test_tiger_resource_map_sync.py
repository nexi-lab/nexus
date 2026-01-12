"""Integration tests for Tiger resource map sync from metadata (#934).

Tests that tiger_resource_map is populated from existing files on startup,
enabling Tiger Cache to provide O(1) permission lookups for pre-existing files.

Related: Issue #934

Note: Tiger Cache is only enabled for PostgreSQL, not SQLite, due to lock
contention issues. Tests that require Tiger Cache sync functionality are
skipped when using SQLite.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import text

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext

# Tiger Cache is only available with PostgreSQL (SQLite has lock contention issues)
requires_tiger_cache = pytest.mark.skip(
    reason="Tiger Cache requires PostgreSQL (disabled on SQLite due to lock contention)"
)


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def admin_context() -> dict:
    """Admin context for file operations."""
    return {"user": "admin", "groups": [], "is_admin": True, "is_system": False}


class TestTigerResourceMapSync:
    """Test that tiger_resource_map is synced from metadata on startup.

    Note: These tests are skipped on SQLite because Tiger Cache is only
    enabled for PostgreSQL due to lock contention issues.
    """

    @requires_tiger_cache
    def test_sync_populates_resource_map_on_init(self, temp_dir: Path, admin_context: dict) -> None:
        """Test that _sync_resource_map_from_metadata() populates the map."""
        db_path = temp_dir / "metadata.db"

        # PHASE 1: Create NexusFS and add files (simulates existing data)
        # Disable sync for initial creation to simulate pre-existing data
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "false"

        nx1 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        # Create test files
        ctx = OperationContext(**admin_context)
        nx1.write("/workspace/file1.txt", b"content1", context=ctx)
        nx1.write("/workspace/file2.txt", b"content2", context=ctx)
        nx1.write("/docs/readme.md", b"# README", context=ctx)

        # Verify files exist
        files = nx1.list("/", recursive=True, context=ctx)
        assert len(files) == 3

        # Close first instance
        nx1.close()

        # PHASE 2: Create NEW NexusFS instance with sync enabled
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        nx2 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=True,
        )

        # VERIFY: tiger_resource_map should now contain all 3 files
        with nx2.metadata.engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM tiger_resource_map"))
            count = result.scalar()
            assert count >= 3, f"Expected at least 3 resources in map, got {count}"

            # Verify specific files are mapped
            result = conn.execute(
                text(
                    """
                SELECT resource_id FROM tiger_resource_map
                WHERE resource_type = 'file'
                ORDER BY resource_id
            """
                )
            )
            paths = [row[0] for row in result]
            assert "/workspace/file1.txt" in paths
            assert "/workspace/file2.txt" in paths
            assert "/docs/readme.md" in paths

        nx2.close()

    @requires_tiger_cache
    def test_sync_with_tenant_id(self, temp_dir: Path, admin_context: dict) -> None:
        """Test that sync uses tenant_id from file metadata.

        Note: tenant_id column was intentionally removed from tiger_resource_map
        since resource paths are globally unique.
        """
        db_path = temp_dir / "metadata.db"
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "false"

        # Create file with specific tenant
        nx1 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
            tenant_id="acme_corp",
        )

        ctx = OperationContext(user="user1", groups=[], tenant_id="acme_corp", is_admin=True)
        nx1.write("/acme/document.txt", b"acme content", context=ctx)
        nx1.close()

        # Restart with sync
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        nx2 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=True,
            tenant_id="acme_corp",
        )

        # Verify resource is in map with correct tenant
        with nx2.metadata.engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT tenant_id FROM tiger_resource_map
                WHERE resource_id = '/acme/document.txt'
            """
                )
            )
            row = result.fetchone()
            assert row is not None, "File should be in resource map"
            # Tenant should be either the file's tenant or "default"
            assert row[0] in ("acme_corp", "default")

        nx2.close()

    @requires_tiger_cache
    def test_sync_is_idempotent(self, temp_dir: Path, admin_context: dict) -> None:
        """Test that calling sync multiple times doesn't create duplicates."""
        db_path = temp_dir / "metadata.db"
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        nx = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        ctx = OperationContext(**admin_context)
        nx.write("/file.txt", b"content", context=ctx)

        # Manually call sync multiple times
        count1 = nx._sync_resource_map_from_metadata()
        count2 = nx._sync_resource_map_from_metadata()
        count3 = nx._sync_resource_map_from_metadata()

        # All calls should report same count (idempotent)
        assert count1 == count2 == count3 == 1

        # Verify only one entry in database
        with nx.metadata.engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT COUNT(*) FROM tiger_resource_map
                WHERE resource_id = '/file.txt'
            """
                )
            )
            assert result.scalar() == 1, "Should have exactly one entry"

        nx.close()

    def test_sync_disabled_by_env_var(self, temp_dir: Path, admin_context: dict) -> None:
        """Test that sync can be disabled via environment variable."""
        db_path = temp_dir / "metadata.db"

        # Create file first with sync disabled
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "false"

        nx1 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        ctx = OperationContext(**admin_context)
        nx1.write("/file.txt", b"content", context=ctx)

        # Check resource map is empty (sync was disabled)
        with nx1.metadata.engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT COUNT(*) FROM tiger_resource_map
                WHERE resource_id = '/file.txt'
            """
                )
            )
            count = result.scalar()
            assert count == 0, "Sync was disabled, map should be empty"

        nx1.close()

    def test_sync_with_empty_database(self, temp_dir: Path) -> None:
        """Test that sync handles empty database gracefully."""
        db_path = temp_dir / "metadata.db"
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        nx = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        # Sync should return 0 for empty database
        count = nx._sync_resource_map_from_metadata()
        assert count == 0

        nx.close()

    @requires_tiger_cache
    def test_sync_with_many_files(self, temp_dir: Path, admin_context: dict) -> None:
        """Test sync performance with many files."""
        db_path = temp_dir / "metadata.db"
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "false"

        nx1 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        # Create 100 files
        num_files = 100
        ctx = OperationContext(**admin_context)
        for i in range(num_files):
            nx1.write(f"/files/file_{i:03d}.txt", f"content {i}".encode(), context=ctx)

        nx1.close()

        # Restart with sync
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        nx2 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=True,
        )

        # Verify all files synced
        with nx2.metadata.engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM tiger_resource_map"))
            count = result.scalar()
            assert count >= num_files, f"Expected {num_files} resources, got {count}"

        nx2.close()


class TestTigerCacheAfterSync:
    """Test that Tiger Cache works correctly after resource map sync."""

    def test_tiger_check_access_after_sync(self, temp_dir: Path, admin_context: dict) -> None:
        """Test that Tiger Cache can check access after sync."""
        db_path = temp_dir / "metadata.db"
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        nx = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=True,
            tenant_id="test_tenant",
        )

        # Grant admin ownership FIRST (before writing)
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", "/"),
            tenant_id="test_tenant",
            context=admin_context,
        )

        # Create file (admin now has permission)
        ctx = OperationContext(**admin_context, tenant_id="test_tenant")
        nx.write("/doc.txt", b"test content", context=ctx)

        # Grant alice read access
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
            tenant_id="test_tenant",
            context=admin_context,
        )

        # Verify resource is in tiger_resource_map
        if nx._rebac_manager._tiger_cache:
            resource_map = nx._rebac_manager._tiger_cache._resource_map
            int_id = resource_map.get_or_create_int_id("file", "/doc.txt", "test_tenant")
            assert int_id > 0, "Resource should have an integer ID"

        nx.close()

    def test_resource_map_survives_restart(self, temp_dir: Path, admin_context: dict) -> None:
        """Test that resource map data persists across restarts."""
        db_path = temp_dir / "metadata.db"
        os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

        # First instance - create file (sync runs)
        nx1 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        ctx = OperationContext(**admin_context)
        nx1.write("/persistent.txt", b"data", context=ctx)

        # Get the int_id from first instance
        int_id_1 = None
        if nx1._rebac_manager._tiger_cache:
            resource_map = nx1._rebac_manager._tiger_cache._resource_map
            int_id_1 = resource_map.get_or_create_int_id("file", "/persistent.txt", "default")

        nx1.close()

        # Second instance - verify same int_id
        nx2 = NexusFS(
            backend=LocalBackend(temp_dir / "data"),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        if nx2._rebac_manager._tiger_cache:
            resource_map = nx2._rebac_manager._tiger_cache._resource_map
            int_id_2 = resource_map.get_or_create_int_id("file", "/persistent.txt", "default")
            assert int_id_1 == int_id_2, "Int ID should be same across restarts"

        nx2.close()
