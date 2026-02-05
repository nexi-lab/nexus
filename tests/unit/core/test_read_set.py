"""Unit tests for Read Set Tracking (Issue #1166).

Tests for ReadSetEntry, ReadSet, and ReadSetRegistry classes that enable
precise cache invalidation and efficient subscription updates.
"""

import time

from nexus.core.read_set import (
    AccessType,
    ReadSet,
    ReadSetEntry,
    ReadSetRegistry,
    ResourceType,
    get_global_registry,
    set_global_registry,
)


class TestReadSetEntry:
    """Tests for ReadSetEntry dataclass."""

    def test_create_basic_entry(self):
        """Test creating a basic read set entry."""
        entry = ReadSetEntry(
            resource_type="file",
            resource_id="/inbox/message.txt",
            revision=42,
        )
        assert entry.resource_type == "file"
        assert entry.resource_id == "/inbox/message.txt"
        assert entry.revision == 42
        assert entry.access_type == AccessType.CONTENT
        assert entry.timestamp > 0

    def test_create_entry_with_access_type(self):
        """Test creating entry with specific access type."""
        entry = ReadSetEntry(
            resource_type="directory",
            resource_id="/inbox/",
            revision=10,
            access_type=AccessType.LIST,
        )
        assert entry.access_type == AccessType.LIST

    def test_is_stale_when_newer_revision(self):
        """Test staleness detection when resource has newer revision."""
        entry = ReadSetEntry(
            resource_type="file",
            resource_id="/test.txt",
            revision=10,
        )
        assert entry.is_stale(current_revision=15) is True
        assert entry.is_stale(current_revision=10) is False
        assert entry.is_stale(current_revision=5) is False

    def test_to_dict_and_from_dict(self):
        """Test serialization roundtrip."""
        entry = ReadSetEntry(
            resource_type="file",
            resource_id="/test.txt",
            revision=42,
            access_type=AccessType.METADATA,
        )
        data = entry.to_dict()

        assert data["resource_type"] == "file"
        assert data["resource_id"] == "/test.txt"
        assert data["revision"] == 42
        assert data["access_type"] == "metadata"

        # Roundtrip
        restored = ReadSetEntry.from_dict(data)
        assert restored.resource_type == entry.resource_type
        assert restored.resource_id == entry.resource_id
        assert restored.revision == entry.revision


class TestReadSet:
    """Tests for ReadSet class."""

    def test_create_empty_read_set(self):
        """Test creating an empty read set."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        assert rs.query_id == "q1"
        assert rs.zone_id == "t1"
        assert len(rs) == 0
        assert len(rs.entries) == 0

    def test_record_read(self):
        """Test recording a read operation."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        entry = rs.record_read("file", "/test.txt", 10)

        assert len(rs) == 1
        assert entry.resource_type == "file"
        assert entry.resource_id == "/test.txt"
        assert entry.revision == 10

    def test_record_multiple_reads(self):
        """Test recording multiple read operations."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("file", "/a.txt", 10)
        rs.record_read("file", "/b.txt", 15)
        rs.record_read("directory", "/inbox/", 5)

        assert len(rs) == 3

    def test_overlaps_with_write_direct_match(self):
        """Test overlap detection for direct path match."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("file", "/inbox/a.txt", 10)

        # Write to the same file with newer revision
        assert rs.overlaps_with_write("/inbox/a.txt", 15) is True

        # Write to the same file but older revision
        assert rs.overlaps_with_write("/inbox/a.txt", 5) is False

        # Write to different file
        assert rs.overlaps_with_write("/inbox/b.txt", 15) is False

    def test_overlaps_with_write_directory_containment(self):
        """Test overlap detection for directory containment."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("directory", "/inbox/", 5, access_type=AccessType.LIST)

        # Write to file inside the directory
        assert rs.overlaps_with_write("/inbox/new.txt", 10) is True
        assert rs.overlaps_with_write("/inbox/subdir/file.txt", 10) is True

        # Write to file outside the directory
        assert rs.overlaps_with_write("/docs/file.txt", 10) is False

    def test_overlaps_with_write_no_overlap(self):
        """Test overlap detection when there's no overlap."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("file", "/inbox/a.txt", 10)
        rs.record_read("file", "/inbox/b.txt", 15)

        assert rs.overlaps_with_write("/docs/x.txt", 20) is False
        assert rs.overlaps_with_write("/outbox/y.txt", 20) is False

    def test_get_affected_entries(self):
        """Test getting affected entries by a write."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("file", "/inbox/a.txt", 10)
        rs.record_read("file", "/inbox/b.txt", 15)
        rs.record_read("directory", "/inbox/", 5, access_type=AccessType.LIST)

        # Write to a.txt affects direct entry and directory
        affected = rs.get_affected_entries("/inbox/a.txt", 20)
        assert len(affected) == 2

        # Write to new file in inbox affects only directory
        affected = rs.get_affected_entries("/inbox/new.txt", 20)
        assert len(affected) == 1
        assert affected[0].resource_type == "directory"

    def test_factory_create(self):
        """Test factory method for creating read sets."""
        rs = ReadSet.create(zone_id="org_acme")
        assert rs.zone_id == "org_acme"
        assert len(rs.query_id) > 0  # UUID generated
        assert rs.expires_at is None

    def test_factory_create_with_ttl(self):
        """Test factory method with TTL."""
        rs = ReadSet.create(zone_id="t1", ttl_seconds=60.0)
        assert rs.expires_at is not None
        assert rs.expires_at > rs.created_at

    def test_is_expired(self):
        """Test expiration checking."""
        # Non-expiring read set
        rs1 = ReadSet.create(zone_id="t1")
        assert rs1.is_expired() is False

        # Expired read set (TTL in the past)
        rs2 = ReadSet(
            query_id="q2",
            zone_id="t1",
            created_at=time.time() - 120,
            expires_at=time.time() - 60,
        )
        assert rs2.is_expired() is True

    def test_to_dict_and_from_dict(self):
        """Test serialization roundtrip."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("file", "/a.txt", 10)
        rs.record_read("directory", "/inbox/", 5, access_type=AccessType.LIST)

        data = rs.to_dict()
        restored = ReadSet.from_dict(data)

        assert restored.query_id == rs.query_id
        assert restored.zone_id == rs.zone_id
        assert len(restored) == len(rs)

    def test_iteration(self):
        """Test iterating over entries."""
        rs = ReadSet(query_id="q1", zone_id="t1")
        rs.record_read("file", "/a.txt", 10)
        rs.record_read("file", "/b.txt", 15)

        paths = [entry.resource_id for entry in rs]
        assert "/a.txt" in paths
        assert "/b.txt" in paths


class TestReadSetRegistry:
    """Tests for ReadSetRegistry class."""

    def setup_method(self):
        """Create a fresh registry for each test."""
        self.registry = ReadSetRegistry()

    def test_register_and_get_read_set(self):
        """Test registering and retrieving a read set."""
        rs = ReadSet(query_id="sub_1", zone_id="t1")
        rs.record_read("file", "/inbox/a.txt", 10)

        self.registry.register(rs)
        retrieved = self.registry.get_read_set("sub_1")

        assert retrieved is not None
        assert retrieved.query_id == "sub_1"
        assert len(retrieved) == 1

    def test_register_updates_existing(self):
        """Test that registering with same ID updates existing entry."""
        rs1 = ReadSet(query_id="sub_1", zone_id="t1")
        rs1.record_read("file", "/a.txt", 10)
        self.registry.register(rs1)

        rs2 = ReadSet(query_id="sub_1", zone_id="t1")
        rs2.record_read("file", "/b.txt", 20)
        self.registry.register(rs2)

        retrieved = self.registry.get_read_set("sub_1")
        assert len(retrieved) == 1
        assert retrieved.entries[0].resource_id == "/b.txt"

    def test_unregister(self):
        """Test unregistering a read set."""
        rs = ReadSet(query_id="sub_1", zone_id="t1")
        rs.record_read("file", "/a.txt", 10)
        self.registry.register(rs)

        assert self.registry.unregister("sub_1") is True
        assert self.registry.get_read_set("sub_1") is None
        assert self.registry.unregister("sub_1") is False  # Already removed

    def test_get_affected_queries_direct_match(self):
        """Test finding affected queries with direct path match."""
        rs = ReadSet(query_id="sub_1", zone_id="t1")
        rs.record_read("file", "/inbox/a.txt", 10)
        self.registry.register(rs)

        affected = self.registry.get_affected_queries("/inbox/a.txt", 15)
        assert "sub_1" in affected

        affected = self.registry.get_affected_queries("/inbox/b.txt", 15)
        assert "sub_1" not in affected

    def test_get_affected_queries_directory_containment(self):
        """Test finding affected queries with directory containment."""
        rs = ReadSet(query_id="sub_1", zone_id="t1")
        rs.record_read("directory", "/inbox/", 5, access_type=AccessType.LIST)
        self.registry.register(rs)

        # New file in /inbox/ affects the query
        affected = self.registry.get_affected_queries("/inbox/new.txt", 10)
        assert "sub_1" in affected

        # File in /docs/ doesn't affect
        affected = self.registry.get_affected_queries("/docs/other.txt", 10)
        assert "sub_1" not in affected

    def test_get_affected_queries_multiple_subscriptions(self):
        """Test finding affected queries with multiple subscriptions."""
        rs1 = ReadSet(query_id="sub_1", zone_id="t1")
        rs1.record_read("file", "/inbox/a.txt", 10)
        self.registry.register(rs1)

        rs2 = ReadSet(query_id="sub_2", zone_id="t1")
        rs2.record_read("directory", "/inbox/", 5, access_type=AccessType.LIST)
        self.registry.register(rs2)

        rs3 = ReadSet(query_id="sub_3", zone_id="t1")
        rs3.record_read("file", "/docs/readme.md", 20)
        self.registry.register(rs3)

        # Write to /inbox/a.txt affects sub_1 (direct) and sub_2 (directory)
        affected = self.registry.get_affected_queries("/inbox/a.txt", 15)
        assert "sub_1" in affected
        assert "sub_2" in affected
        assert "sub_3" not in affected

    def test_get_affected_queries_with_zone_filter(self):
        """Test finding affected queries with zone filter."""
        rs1 = ReadSet(query_id="sub_1", zone_id="zone_a")
        rs1.record_read("file", "/shared/data.txt", 10)
        self.registry.register(rs1)

        rs2 = ReadSet(query_id="sub_2", zone_id="zone_b")
        rs2.record_read("file", "/shared/data.txt", 10)
        self.registry.register(rs2)

        # Without zone filter, both affected
        affected = self.registry.get_affected_queries("/shared/data.txt", 15)
        assert "sub_1" in affected
        assert "sub_2" in affected

        # With zone filter, only matching zone
        affected = self.registry.get_affected_queries("/shared/data.txt", 15, zone_id="zone_a")
        assert "sub_1" in affected
        assert "sub_2" not in affected

    def test_get_queries_for_zone(self):
        """Test getting all queries for a zone."""
        rs1 = ReadSet(query_id="sub_1", zone_id="t1")
        rs1.record_read("file", "/a.txt", 10)
        self.registry.register(rs1)

        rs2 = ReadSet(query_id="sub_2", zone_id="t1")
        rs2.record_read("file", "/b.txt", 10)
        self.registry.register(rs2)

        rs3 = ReadSet(query_id="sub_3", zone_id="t2")
        rs3.record_read("file", "/c.txt", 10)
        self.registry.register(rs3)

        queries = self.registry.get_queries_for_zone("t1")
        assert "sub_1" in queries
        assert "sub_2" in queries
        assert "sub_3" not in queries

    def test_cleanup_expired(self):
        """Test cleaning up expired read sets."""
        # Non-expiring read set
        rs1 = ReadSet.create(zone_id="t1")
        rs1.record_read("file", "/a.txt", 10)
        # Manually set query_id for test
        rs1 = ReadSet(query_id="sub_1", zone_id="t1")
        rs1.record_read("file", "/a.txt", 10)
        self.registry.register(rs1)

        # Expired read set
        rs2 = ReadSet(
            query_id="sub_2",
            zone_id="t1",
            created_at=time.time() - 120,
            expires_at=time.time() - 60,
        )
        rs2.record_read("file", "/b.txt", 10)
        self.registry.register(rs2)

        assert len(self.registry) == 2

        removed = self.registry.cleanup_expired()
        assert removed == 1
        assert len(self.registry) == 1
        assert self.registry.get_read_set("sub_1") is not None
        assert self.registry.get_read_set("sub_2") is None

    def test_clear(self):
        """Test clearing all read sets."""
        rs1 = ReadSet(query_id="sub_1", zone_id="t1")
        rs1.record_read("file", "/a.txt", 10)
        self.registry.register(rs1)

        rs2 = ReadSet(query_id="sub_2", zone_id="t1")
        rs2.record_read("file", "/b.txt", 10)
        self.registry.register(rs2)

        assert len(self.registry) == 2

        self.registry.clear()
        assert len(self.registry) == 0

    def test_get_stats(self):
        """Test getting registry statistics."""
        rs = ReadSet(query_id="sub_1", zone_id="t1")
        rs.record_read("file", "/a.txt", 10)
        rs.record_read("directory", "/inbox/", 5, access_type=AccessType.LIST)
        self.registry.register(rs)

        self.registry.get_affected_queries("/a.txt", 15)
        self.registry.get_affected_queries("/inbox/new.txt", 15)

        stats = self.registry.get_stats()
        assert stats["read_sets_count"] == 1
        assert stats["paths_indexed"] == 2
        assert stats["directories_indexed"] == 1
        assert stats["registers"] == 1
        assert stats["lookups"] == 2


class TestGlobalRegistry:
    """Tests for global registry singleton."""

    def teardown_method(self):
        """Clear global registry after each test."""
        set_global_registry(None)

    def test_get_global_registry_creates_singleton(self):
        """Test that get_global_registry creates a singleton."""
        registry1 = get_global_registry()
        registry2 = get_global_registry()
        assert registry1 is registry2

    def test_set_global_registry(self):
        """Test setting the global registry."""
        custom_registry = ReadSetRegistry()
        set_global_registry(custom_registry)

        assert get_global_registry() is custom_registry

    def test_set_global_registry_to_none(self):
        """Test clearing the global registry."""
        get_global_registry()  # Create the singleton
        set_global_registry(None)

        # Should create a new one
        new_registry = get_global_registry()
        assert new_registry is not None


class TestAccessTypeEnum:
    """Tests for AccessType enum."""

    def test_access_type_values(self):
        """Test AccessType enum values."""
        assert AccessType.CONTENT == "content"
        assert AccessType.METADATA == "metadata"
        assert AccessType.LIST == "list"
        assert AccessType.EXISTS == "exists"


class TestResourceTypeEnum:
    """Tests for ResourceType enum."""

    def test_resource_type_values(self):
        """Test ResourceType enum values."""
        assert ResourceType.FILE == "file"
        assert ResourceType.DIRECTORY == "directory"
        assert ResourceType.METADATA == "metadata"
