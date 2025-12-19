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
from sqlalchemy import create_engine, text

from nexus.core.rebac_manager_enhanced import EnhancedReBACManager
from nexus.core.tiger_cache import (
    ROARING_AVAILABLE,
    Bitmap,
    PythonBitmap,
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


class TestPythonBitmap:
    """Tests for the fallback Python bitmap implementation."""

    def test_basic_operations(self):
        """Test basic bitmap operations."""
        bitmap = PythonBitmap()
        assert len(bitmap) == 0

        bitmap.add(1)
        bitmap.add(5)
        bitmap.add(10)

        assert len(bitmap) == 3
        assert 1 in bitmap
        assert 5 in bitmap
        assert 10 in bitmap
        assert 2 not in bitmap

    def test_remove(self):
        """Test removing elements."""
        bitmap = PythonBitmap({1, 2, 3})
        bitmap.remove(2)
        assert 2 not in bitmap
        assert 1 in bitmap
        assert 3 in bitmap

    def test_set_operations(self):
        """Test intersection and union."""
        a = PythonBitmap({1, 2, 3})
        b = PythonBitmap({2, 3, 4})

        intersection = a & b
        assert set(intersection) == {2, 3}

        union = a | b
        assert set(union) == {1, 2, 3, 4}

    def test_serialize_deserialize(self):
        """Test serialization round-trip."""
        original = PythonBitmap({1, 5, 100, 1000})
        serialized = original.serialize()

        restored = PythonBitmap.deserialize(serialized)
        assert set(restored) == {1, 5, 100, 1000}


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


class TestBitmapCompatibility:
    """Test compatibility between Roaring and Python bitmaps."""

    @pytest.mark.skipif(not ROARING_AVAILABLE, reason="pyroaring not installed")
    def test_roaring_available(self):
        """Test that Roaring is detected when installed."""
        assert ROARING_AVAILABLE is True

    def test_bitmap_interface_consistent(self):
        """Test that Bitmap has consistent interface."""
        bitmap = Bitmap()
        bitmap.add(1)
        bitmap.add(2)

        assert 1 in bitmap
        assert 2 in bitmap
        assert 3 not in bitmap

        # Serialization works
        data = bitmap.serialize()
        assert isinstance(data, bytes)
