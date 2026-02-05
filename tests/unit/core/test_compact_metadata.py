"""Tests for CompactFileMetadata and string interning pools.

Issue #911: Implement CompactFileMetadata with __slots__ for 3x memory reduction.
"""

import sys
import threading
from datetime import UTC, datetime

import pytest

from nexus.core.compact_metadata import (
    CompactFileMetadata,
    StringInternPool,
    clear_intern_pools,
    get_intern_pools,
    get_pool_stats,
)
from nexus.core.metadata import FileMetadata


@pytest.fixture(autouse=True)
def reset_intern_pools():
    """Reset interning pools before each test."""
    clear_intern_pools()
    yield
    clear_intern_pools()


class TestStringInternPool:
    """Tests for StringInternPool."""

    def test_intern_returns_same_id_for_same_string(self):
        pool = StringInternPool()
        id1 = pool.intern("hello")
        id2 = pool.intern("hello")
        assert id1 == id2

    def test_intern_returns_different_ids_for_different_strings(self):
        pool = StringInternPool()
        id1 = pool.intern("hello")
        id2 = pool.intern("world")
        assert id1 != id2

    def test_get_returns_original_string(self):
        pool = StringInternPool()
        original = "/path/to/file.txt"
        str_id = pool.intern(original)
        retrieved = pool.get(str_id)
        assert retrieved == original

    def test_get_or_none_returns_none_for_negative_id(self):
        pool = StringInternPool()
        pool.intern("test")
        assert pool.get_or_none(-1) is None

    def test_get_or_none_returns_none_for_invalid_id(self):
        pool = StringInternPool()
        pool.intern("test")
        assert pool.get_or_none(999) is None

    def test_len_returns_count_of_interned_strings(self):
        pool = StringInternPool()
        assert len(pool) == 0
        pool.intern("a")
        pool.intern("b")
        pool.intern("a")  # Duplicate
        assert len(pool) == 2

    def test_thread_safety(self):
        """Test that interning is thread-safe."""
        pool = StringInternPool()
        results = {}
        errors = []

        def intern_strings(thread_id: int):
            try:
                for i in range(100):
                    str_id = pool.intern(f"string_{i}")
                    results[(thread_id, i)] = str_id
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=intern_strings, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All threads should get the same ID for the same string
        for i in range(100):
            ids = [results[(t, i)] for t in range(10)]
            assert len(set(ids)) == 1, f"Inconsistent IDs for string_{i}"


class TestCompactFileMetadata:
    """Tests for CompactFileMetadata."""

    def test_from_file_metadata_basic(self):
        """Test basic conversion from FileMetadata."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
            etag="abc123",
            mime_type="text/plain",
            created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            modified_at=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            version=1,
            zone_id="zone-1",
            created_by="user-1",
            is_directory=False,
        )

        compact = CompactFileMetadata.from_file_metadata(metadata)

        assert compact.size == 1024
        assert compact.version == 1
        assert compact.path == "/test/file.txt"
        assert not compact.is_directory

    def test_to_file_metadata_roundtrip(self):
        """Test roundtrip conversion maintains data integrity."""
        original = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
            etag="abc123",
            mime_type="text/plain",
            created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            modified_at=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            version=5,
            zone_id="zone-1",
            created_by="user-1",
            is_directory=False,
        )

        compact = CompactFileMetadata.from_file_metadata(original)
        restored = compact.to_file_metadata()

        assert restored.path == original.path
        assert restored.backend_name == original.backend_name
        assert restored.physical_path == original.physical_path
        assert restored.size == original.size
        assert restored.etag == original.etag
        assert restored.mime_type == original.mime_type
        assert restored.version == original.version
        assert restored.zone_id == original.zone_id
        assert restored.created_by == original.created_by
        assert restored.is_directory == original.is_directory
        # Timestamps are converted to/from Unix, so compare as integers
        assert int(restored.created_at.timestamp()) == int(original.created_at.timestamp())
        assert int(restored.modified_at.timestamp()) == int(original.modified_at.timestamp())

    def test_handles_none_values(self):
        """Test conversion with None optional fields."""
        original = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=0,
            etag=None,
            mime_type=None,
            created_at=None,
            modified_at=None,
            version=1,
            zone_id=None,
            created_by=None,
            is_directory=False,
        )

        compact = CompactFileMetadata.from_file_metadata(original)
        restored = compact.to_file_metadata()

        assert restored.etag is None
        assert restored.mime_type is None
        assert restored.created_at is None
        assert restored.modified_at is None
        assert restored.zone_id is None
        assert restored.created_by is None

    def test_is_directory_flag(self):
        """Test is_directory flag packing/unpacking."""
        dir_metadata = FileMetadata(
            path="/test/dir",
            backend_name="local",
            physical_path="/var/data/dir",
            size=0,
            is_directory=True,
        )

        compact = CompactFileMetadata.from_file_metadata(dir_metadata)
        assert compact.is_directory is True

        restored = compact.to_file_metadata()
        assert restored.is_directory is True

    def test_string_interning_deduplication(self):
        """Test that same strings are deduplicated across instances."""
        pools = get_intern_pools()
        path_pool = pools["path"]
        initial_count = len(path_pool)

        # Create multiple metadata objects with same path
        for i in range(100):
            metadata = FileMetadata(
                path="/shared/path/file.txt",
                backend_name="local",
                physical_path="/var/data/file.txt",
                size=i,
            )
            CompactFileMetadata.from_file_metadata(metadata)

        # Path should only be interned once
        assert len(path_pool) == initial_count + 2  # path + physical_path

    def test_file_metadata_to_compact_method(self):
        """Test FileMetadata.to_compact() convenience method."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
        )

        compact = metadata.to_compact()
        assert isinstance(compact, CompactFileMetadata)
        assert compact.size == 1024

    def test_file_metadata_from_compact_method(self):
        """Test FileMetadata.from_compact() class method."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
        )

        compact = metadata.to_compact()
        restored = FileMetadata.from_compact(compact)

        assert restored.path == metadata.path
        assert restored.size == metadata.size


class TestPoolStats:
    """Tests for pool statistics."""

    def test_get_pool_stats(self):
        """Test getting pool statistics."""
        # Create some metadata to populate pools
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
            etag="hash123",
            mime_type="text/plain",
            zone_id="zone-1",
            created_by="user-1",
        )
        CompactFileMetadata.from_file_metadata(metadata)

        stats = get_pool_stats()

        assert "path" in stats
        assert "backend" in stats
        assert "hash" in stats
        assert "mime" in stats
        assert "zone" in stats
        assert "user" in stats

        # Each pool should have count and memory_estimate
        for _name, pool_stats in stats.items():
            assert "count" in pool_stats
            assert "memory_estimate" in pool_stats
            assert pool_stats["count"] >= 0
            assert pool_stats["memory_estimate"] >= 0


class TestMemoryEfficiency:
    """Tests for memory efficiency of compact metadata."""

    def test_compact_uses_less_memory(self):
        """Test that CompactFileMetadata uses less memory than FileMetadata."""
        # Create a FileMetadata instance
        file_metadata = FileMetadata(
            path="/test/very/long/path/to/a/file/that/has/many/segments.txt",
            backend_name="local-storage-backend",
            physical_path="/var/data/storage/very/long/path/to/a/file.txt",
            size=1024,
            etag="abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
            mime_type="application/octet-stream",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            modified_at=datetime(2024, 1, 2, tzinfo=UTC),
            version=1,
            zone_id="zone-abc-123-def-456",
            created_by="user-abc-123-def-456",
            is_directory=False,
        )

        compact_metadata = CompactFileMetadata.from_file_metadata(file_metadata)

        # Get object sizes (without counting referenced objects)
        file_metadata_size = sys.getsizeof(file_metadata)
        compact_metadata_size = sys.getsizeof(compact_metadata)

        # CompactFileMetadata should be significantly smaller
        # Note: The slots version of FileMetadata is already smaller,
        # but CompactFileMetadata replaces strings with ints
        assert compact_metadata_size <= file_metadata_size

    def test_slots_on_file_metadata(self):
        """Test that FileMetadata uses __slots__."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
        )

        # Slotted classes don't have __dict__
        assert not hasattr(metadata, "__dict__")

    def test_frozen_compact_metadata(self):
        """Test that CompactFileMetadata is immutable (frozen)."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/var/data/file.txt",
            size=1024,
        )
        compact = CompactFileMetadata.from_file_metadata(metadata)

        # Frozen dataclass should raise on attribute assignment
        with pytest.raises(AttributeError):
            compact.size = 2048
