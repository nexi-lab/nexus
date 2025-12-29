"""Unit tests for Tiger Cache - materialized permissions as Roaring Bitmaps.

Tests cover:
- Resource mapping (UUID to int64)
- Bitmap operations (get, set, check)
- Cache invalidation
- Queue processing
- Integration with EnhancedReBACManager

Related: Issue #682
"""

import pytest
from pyroaring import BitMap as RoaringBitmap
from sqlalchemy import create_engine, text

from nexus.core.rebac_manager_enhanced import EnhancedReBACManager
from nexus.core.tiger_cache import (
    TigerCache,
    TigerResourceMap,
)
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    # Create additional tables not in models.py (Leopard closure - normally done by migration)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS rebac_group_closure (
                member_type VARCHAR(50) NOT NULL,
                member_id VARCHAR(255) NOT NULL,
                group_type VARCHAR(50) NOT NULL,
                group_id VARCHAR(255) NOT NULL,
                tenant_id VARCHAR(255) NOT NULL,
                depth INTEGER NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (member_type, member_id, group_type, group_id, tenant_id)
            )
        """
            )
        )

    return engine


@pytest.fixture
def resource_map(engine):
    """Create a TigerResourceMap for testing."""
    return TigerResourceMap(engine)


@pytest.fixture
def tiger_cache(engine, resource_map):
    """Create a TigerCache for testing."""
    return TigerCache(engine=engine, resource_map=resource_map)


@pytest.fixture
def manager(engine):
    """Create an EnhancedReBACManager with Tiger Cache enabled."""
    mgr = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_tenant_isolation=False,  # Simplify tests
        enable_graph_limits=True,
        enable_leopard=True,
        enable_tiger_cache=True,
    )
    yield mgr
    mgr.close()


@pytest.fixture
def manager_no_tiger(engine):
    """Create an EnhancedReBACManager with Tiger Cache disabled."""
    mgr = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_tenant_isolation=False,
        enable_graph_limits=True,
        enable_leopard=True,
        enable_tiger_cache=False,
    )
    yield mgr
    mgr.close()


class TestTigerResourceMap:
    """Tests for the resource UUID to int64 mapping."""

    def test_get_or_create_int_id(self, resource_map):
        """Test creating and retrieving integer IDs."""
        # First call creates
        id1 = resource_map.get_or_create_int_id("file", "file-uuid-1", "tenant1")
        assert id1 > 0

        # Second call retrieves same ID
        id2 = resource_map.get_or_create_int_id("file", "file-uuid-1", "tenant1")
        assert id2 == id1

        # Different resource gets different ID
        id3 = resource_map.get_or_create_int_id("file", "file-uuid-2", "tenant1")
        assert id3 != id1

    def test_tenant_isolation(self, resource_map):
        """Test that resource IDs are tenant-isolated."""
        id1 = resource_map.get_or_create_int_id("file", "file-uuid-1", "tenant1")
        id2 = resource_map.get_or_create_int_id("file", "file-uuid-1", "tenant2")

        # Same resource ID in different tenants gets different int IDs
        assert id1 != id2

    def test_get_resource_id(self, resource_map):
        """Test reverse lookup from int ID to resource info."""
        int_id = resource_map.get_or_create_int_id("file", "my-file", "tenant1")

        info = resource_map.get_resource_id(int_id)
        assert info == ("file", "my-file", "tenant1")

    def test_memory_cache(self, resource_map):
        """Test that mappings are cached in memory."""
        int_id = resource_map.get_or_create_int_id("file", "cached-file", "tenant1")

        # Check it's in memory cache
        assert ("file", "cached-file", "tenant1") in resource_map._uuid_to_int
        assert int_id in resource_map._int_to_uuid

        # Clear cache
        resource_map.clear_cache()

        assert ("file", "cached-file", "tenant1") not in resource_map._uuid_to_int
        assert int_id not in resource_map._int_to_uuid


class TestTigerCache:
    """Tests for the Tiger Cache bitmap storage."""

    def test_update_and_check(self, tiger_cache, resource_map):
        """Test updating cache and checking access."""
        # Create some resources
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")
        _r3 = resource_map.get_or_create_int_id("file", "file3", "tenant1")  # noqa: F841

        # Update cache with accessible resources
        tiger_cache.update_cache(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1, r2},  # Alice can read file1 and file2
            revision=1,
        )

        # Check access
        assert tiger_cache.check_access("user", "alice", "read", "file", "file1", "tenant1") is True
        assert tiger_cache.check_access("user", "alice", "read", "file", "file2", "tenant1") is True
        assert (
            tiger_cache.check_access("user", "alice", "read", "file", "file3", "tenant1") is False
        )

    def test_get_accessible_resources(self, tiger_cache, resource_map):
        """Test retrieving all accessible resources."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")

        tiger_cache.update_cache(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1, r2},
            revision=1,
        )

        accessible = tiger_cache.get_accessible_resources(
            "user", "alice", "read", "file", "tenant1"
        )
        assert accessible == {r1, r2}

    def test_cache_miss_returns_none(self, tiger_cache, resource_map):
        """Test that cache miss returns None (not False)."""
        resource_map.get_or_create_int_id("file", "uncached-file", "tenant1")

        result = tiger_cache.check_access(
            "user", "nobody", "read", "file", "uncached-file", "tenant1"
        )
        assert result is None  # Not in cache

    def test_invalidate(self, tiger_cache, resource_map):
        """Test cache invalidation."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")

        tiger_cache.update_cache(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1},
            revision=1,
        )

        # Verify it's cached
        assert tiger_cache.check_access("user", "alice", "read", "file", "file1", "tenant1") is True

        # Invalidate
        count = tiger_cache.invalidate(subject_type="user", subject_id="alice")
        assert count == 1

        # Now should be cache miss
        assert tiger_cache.check_access("user", "alice", "read", "file", "file1", "tenant1") is None


class TestEnhancedReBACManagerWithTiger:
    """Tests for EnhancedReBACManager Tiger Cache integration."""

    def test_tiger_methods_available(self, manager):
        """Test that Tiger Cache methods are available."""
        assert hasattr(manager, "tiger_check_access")
        assert hasattr(manager, "tiger_get_accessible_resources")
        assert hasattr(manager, "tiger_queue_update")
        assert hasattr(manager, "tiger_invalidate_cache")
        assert hasattr(manager, "tiger_register_resource")

    def test_tiger_register_resource(self, manager):
        """Test registering resources."""
        int_id = manager.tiger_register_resource("file", "new-file", "tenant1")
        assert int_id > 0

        # Same resource returns same ID
        int_id2 = manager.tiger_register_resource("file", "new-file", "tenant1")
        assert int_id2 == int_id

    def test_tiger_methods_return_defaults_when_disabled(self, manager_no_tiger):
        """Test that Tiger methods return sensible defaults when disabled."""
        result = manager_no_tiger.tiger_check_access(
            ("user", "alice"), "read", ("file", "file1"), "tenant1"
        )
        assert result is None

        resources = manager_no_tiger.tiger_get_accessible_resources(
            ("user", "alice"), "read", "file", "tenant1"
        )
        assert resources == set()

        queue_id = manager_no_tiger.tiger_queue_update(("user", "alice"), "read", "file", "tenant1")
        assert queue_id is None

        count = manager_no_tiger.tiger_invalidate_cache()
        assert count == 0

    def test_tiger_queue_update(self, manager):
        """Test queuing cache updates."""
        queue_id = manager.tiger_queue_update(
            subject=("user", "alice"),
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            priority=50,
        )

        assert queue_id is not None
        assert queue_id > 0

    def test_tiger_invalidate_by_tenant(self, manager):
        """Test invalidating cache by tenant."""
        # Register some resources
        manager.tiger_register_resource("file", "file1", "tenant1")
        manager.tiger_register_resource("file", "file2", "tenant2")

        # This should not error even with empty cache
        count = manager.tiger_invalidate_cache(tenant_id="tenant1")
        assert count >= 0


class TestTigerCacheIntegration:
    """Integration tests for Tiger Cache with permissions."""

    def test_end_to_end_flow(self, manager):
        """Test the complete flow: create permission, register resource, populate cache."""
        # 1. Create a permission
        manager.rebac_write(
            subject=("user", "alice"),
            relation="owner-of",
            object=("file", "important.txt"),
            tenant_id="tenant1",
        )

        # 2. Register the resource
        int_id = manager.tiger_register_resource("file", "important.txt", "tenant1")
        assert int_id > 0

        # 3. Manually populate cache (in production, done by background worker)
        if manager._tiger_cache:
            manager._tiger_cache.update_cache(
                subject_type="user",
                subject_id="alice",
                permission="owner-of",
                resource_type="file",
                tenant_id="tenant1",
                resource_int_ids={int_id},
                revision=1,
            )

        # 4. Check access via Tiger Cache
        result = manager.tiger_check_access(
            subject=("user", "alice"),
            permission="owner-of",
            object=("file", "important.txt"),
            tenant_id="tenant1",
        )
        assert result is True

        # 5. Check non-existent access
        result = manager.tiger_check_access(
            subject=("user", "bob"),
            permission="owner-of",
            object=("file", "important.txt"),
            tenant_id="tenant1",
        )
        assert result is None  # Not in cache (bob)


class TestTigerCacheIncrementalUpdates:
    """Tests for incremental Tiger Cache updates (Issue #935)."""

    def test_add_to_bitmap_creates_new_bitmap(self, tiger_cache, resource_map):
        """Test that add_to_bitmap creates a new bitmap if none exists."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")

        # Add to bitmap when none exists
        result = tiger_cache.add_to_bitmap(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_id=r1,
        )
        assert result is True

        # Verify resource is now accessible
        assert tiger_cache.check_access("user", "alice", "read", "file", "file1", "tenant1") is True

    def test_add_to_bitmap_updates_existing_bitmap(self, tiger_cache, resource_map):
        """Test that add_to_bitmap adds to an existing bitmap."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")

        # Create initial bitmap with r1
        tiger_cache.update_cache(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1},
            revision=1,
        )

        # Add r2 to bitmap
        result = tiger_cache.add_to_bitmap(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_id=r2,
        )
        assert result is True

        # Verify both resources are accessible
        assert tiger_cache.check_access("user", "alice", "read", "file", "file1", "tenant1") is True
        assert tiger_cache.check_access("user", "alice", "read", "file", "file2", "tenant1") is True

    def test_add_to_bitmap_idempotent(self, tiger_cache, resource_map):
        """Test that adding same resource twice is idempotent."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")

        # Add same resource twice
        tiger_cache.add_to_bitmap("user", "alice", "read", "file", "tenant1", r1)
        tiger_cache.add_to_bitmap("user", "alice", "read", "file", "tenant1", r1)

        # Should still have only 1 resource in bitmap
        accessible = tiger_cache.get_accessible_resources("user", "alice", "read", "file", "tenant1")
        assert accessible == {r1}

    def test_remove_from_bitmap(self, tiger_cache, resource_map):
        """Test removing a resource from bitmap."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")

        # Create bitmap with both resources
        tiger_cache.update_cache(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1, r2},
            revision=1,
        )

        # Remove r1
        result = tiger_cache.remove_from_bitmap(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_id=r1,
        )
        assert result is True

        # Verify r1 is no longer accessible but r2 is
        assert tiger_cache.check_access("user", "alice", "read", "file", "file1", "tenant1") is False
        assert tiger_cache.check_access("user", "alice", "read", "file", "file2", "tenant1") is True

    def test_remove_from_bitmap_not_in_cache(self, tiger_cache, resource_map):
        """Test removing from non-existent bitmap returns False."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")

        # Try to remove from non-existent bitmap
        result = tiger_cache.remove_from_bitmap(
            subject_type="user",
            subject_id="nobody",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_id=r1,
        )
        assert result is False

    def test_add_to_bitmap_bulk(self, tiger_cache, resource_map):
        """Test bulk adding resources to bitmap."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")
        r3 = resource_map.get_or_create_int_id("file", "file3", "tenant1")

        # Bulk add all resources
        added = tiger_cache.add_to_bitmap_bulk(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1, r2, r3},
        )
        assert added == 3

        # Verify all are accessible
        accessible = tiger_cache.get_accessible_resources("user", "alice", "read", "file", "tenant1")
        assert accessible == {r1, r2, r3}

    def test_add_to_bitmap_bulk_partial_duplicates(self, tiger_cache, resource_map):
        """Test bulk add with some resources already in bitmap."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")
        r3 = resource_map.get_or_create_int_id("file", "file3", "tenant1")

        # Create bitmap with r1
        tiger_cache.update_cache(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1},
            revision=1,
        )

        # Bulk add r1, r2, r3 (r1 is duplicate)
        added = tiger_cache.add_to_bitmap_bulk(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids={r1, r2, r3},
        )
        assert added == 2  # Only r2 and r3 are new

        # Verify all are accessible
        accessible = tiger_cache.get_accessible_resources("user", "alice", "read", "file", "tenant1")
        assert accessible == {r1, r2, r3}

    def test_add_to_bitmap_bulk_empty_set(self, tiger_cache):
        """Test bulk add with empty set returns 0."""
        added = tiger_cache.add_to_bitmap_bulk(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            tenant_id="tenant1",
            resource_int_ids=set(),
        )
        assert added == 0

    def test_incremental_updates_different_permissions(self, tiger_cache, resource_map):
        """Test incremental updates maintain separate bitmaps per permission."""
        r1 = resource_map.get_or_create_int_id("file", "file1", "tenant1")
        r2 = resource_map.get_or_create_int_id("file", "file2", "tenant1")

        # Add r1 to read bitmap
        tiger_cache.add_to_bitmap("user", "alice", "read", "file", "tenant1", r1)

        # Add r2 to write bitmap
        tiger_cache.add_to_bitmap("user", "alice", "write", "file", "tenant1", r2)

        # Verify bitmaps are separate
        read_accessible = tiger_cache.get_accessible_resources(
            "user", "alice", "read", "file", "tenant1"
        )
        write_accessible = tiger_cache.get_accessible_resources(
            "user", "alice", "write", "file", "tenant1"
        )

        assert read_accessible == {r1}
        assert write_accessible == {r2}


class TestRoaringBitmap:
    """Test RoaringBitmap operations."""

    def test_bitmap_interface_consistent(self):
        """Test that RoaringBitmap has consistent interface."""
        bitmap = RoaringBitmap()
        bitmap.add(1)
        bitmap.add(2)

        assert 1 in bitmap
        assert 2 in bitmap
        assert 3 not in bitmap

        # Serialization works
        data = bitmap.serialize()
        assert isinstance(data, bytes)
