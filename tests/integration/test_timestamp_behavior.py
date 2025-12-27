"""
Integration tests to verify timestamp behavior in Nexus.

These tests verify:
1. created_at is stable (only set on first creation, never modified)
2. modified_at only updates on real changes (writes), NOT on reads
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import pytest

from nexus import LocalBackend, NexusFS


@pytest.fixture
def nexus_fs(isolated_db, tmp_path):
    """Create a NexusFS instance for testing."""
    backend = LocalBackend(str(tmp_path / "data"))
    nx = NexusFS(backend=backend, db_path=isolated_db, enforce_permissions=False)
    yield nx
    nx.close()


def get_metadata(nexus_fs, path):
    """Get full metadata including created_at from metadata store."""
    return nexus_fs.metadata.get(path)


class TestCreatedAtStability:
    """Tests to verify created_at is stable and never changes after creation."""

    def test_created_at_set_on_first_write(self, nexus_fs):
        """created_at should be set when file is first created."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        before_write = datetime.now(UTC)
        nexus_fs.write(path, b"initial content")
        after_write = datetime.now(UTC)

        meta = get_metadata(nexus_fs, path)

        assert meta.created_at is not None
        # Handle both naive and aware datetimes
        created_at = meta.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        assert before_write <= created_at <= after_write
        print(f"✓ created_at set correctly: {meta.created_at}")

    def test_created_at_preserved_on_update(self, nexus_fs):
        """created_at should NOT change when file is updated."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Initial write
        nexus_fs.write(path, b"initial content")
        meta1 = get_metadata(nexus_fs, path)
        original_created_at = meta1.created_at

        # Wait a bit to ensure timestamps would differ
        time.sleep(0.1)

        # Update the file
        nexus_fs.write(path, b"updated content")
        meta2 = get_metadata(nexus_fs, path)

        assert meta2.created_at == original_created_at
        print(f"✓ created_at preserved after update: {original_created_at} == {meta2.created_at}")

    def test_created_at_preserved_after_multiple_updates(self, nexus_fs):
        """created_at should remain stable across multiple updates."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Initial write
        nexus_fs.write(path, b"v1")
        meta1 = get_metadata(nexus_fs, path)
        original_created_at = meta1.created_at

        # Multiple updates
        for i in range(5):
            time.sleep(0.05)
            nexus_fs.write(path, f"v{i + 2}".encode())

        meta_final = get_metadata(nexus_fs, path)

        assert meta_final.created_at == original_created_at
        print(f"✓ created_at stable after 5 updates: {original_created_at}")


class TestModifiedAtOnReads:
    """Tests to verify modified_at does NOT change on read operations."""

    def test_modified_at_unchanged_after_read(self, nexus_fs):
        """modified_at should NOT change when file is read."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"test content")
        meta1 = get_metadata(nexus_fs, path)
        original_modified_at = meta1.modified_at

        time.sleep(0.1)

        # Read the file
        content = nexus_fs.read(path)
        assert content == b"test content"

        # Check modified_at unchanged
        meta2 = get_metadata(nexus_fs, path)

        assert meta2.modified_at == original_modified_at
        print(f"✓ modified_at unchanged after read: {original_modified_at}")

    def test_modified_at_unchanged_after_multiple_reads(self, nexus_fs):
        """modified_at should NOT change after multiple reads."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"test content for multiple reads")
        meta1 = get_metadata(nexus_fs, path)
        original_modified_at = meta1.modified_at

        # Multiple reads
        for _ in range(10):
            time.sleep(0.02)
            nexus_fs.read(path)

        meta_final = get_metadata(nexus_fs, path)

        assert meta_final.modified_at == original_modified_at
        print(f"✓ modified_at unchanged after 10 reads: {original_modified_at}")

    def test_modified_at_unchanged_after_stat(self, nexus_fs):
        """modified_at should NOT change when stat is called."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"test content")
        meta1 = get_metadata(nexus_fs, path)
        original_modified_at = meta1.modified_at

        time.sleep(0.1)

        # Multiple stat calls
        for _ in range(5):
            nexus_fs.stat(path)

        meta_final = get_metadata(nexus_fs, path)

        assert meta_final.modified_at == original_modified_at
        print(f"✓ modified_at unchanged after stat calls: {original_modified_at}")

    def test_modified_at_unchanged_after_list(self, nexus_fs):
        """modified_at should NOT change when parent directory is listed."""
        path = f"/test/{uuid.uuid4()}/file.txt"
        parent = "/".join(path.split("/")[:-1])

        # Create file
        nexus_fs.write(path, b"test content")
        meta1 = get_metadata(nexus_fs, path)
        original_modified_at = meta1.modified_at

        time.sleep(0.1)

        # List parent directory multiple times
        for _ in range(5):
            list(nexus_fs.list(parent))

        meta_final = get_metadata(nexus_fs, path)

        assert meta_final.modified_at == original_modified_at
        print(f"✓ modified_at unchanged after list calls: {original_modified_at}")


class TestModifiedAtOnWrites:
    """Tests to verify modified_at DOES change on write operations."""

    def test_modified_at_updates_on_write(self, nexus_fs):
        """modified_at SHOULD change when file is written."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"initial content")
        meta1 = get_metadata(nexus_fs, path)
        original_modified_at = meta1.modified_at

        time.sleep(0.1)

        # Update file
        nexus_fs.write(path, b"updated content")
        meta2 = get_metadata(nexus_fs, path)

        assert meta2.modified_at > original_modified_at
        print(f"✓ modified_at updated on write: {original_modified_at} -> {meta2.modified_at}")

    def test_modified_at_updates_each_write(self, nexus_fs):
        """modified_at should update on each write operation."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        timestamps = []

        # Create file
        nexus_fs.write(path, b"v1")
        timestamps.append(get_metadata(nexus_fs, path).modified_at)

        # Multiple writes
        for i in range(3):
            time.sleep(0.1)
            nexus_fs.write(path, f"v{i + 2}".encode())
            timestamps.append(get_metadata(nexus_fs, path).modified_at)

        # Each timestamp should be greater than the previous
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i - 1], f"Timestamp {i} should be > timestamp {i - 1}"

        print(f"✓ modified_at updated on each write: {timestamps}")


class TestTimestampsCombined:
    """Combined tests verifying both created_at and modified_at behavior."""

    def test_read_preserves_both_timestamps(self, nexus_fs):
        """Read operations should preserve both created_at and modified_at."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"test content")
        meta1 = get_metadata(nexus_fs, path)

        time.sleep(0.1)

        # Read file
        nexus_fs.read(path)
        meta2 = get_metadata(nexus_fs, path)

        assert meta2.created_at == meta1.created_at
        assert meta2.modified_at == meta1.modified_at
        print("✓ Both timestamps preserved after read")

    def test_write_updates_only_modified_at(self, nexus_fs):
        """Write operations should update modified_at but NOT created_at."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"initial content")
        meta1 = get_metadata(nexus_fs, path)

        time.sleep(0.1)

        # Update file
        nexus_fs.write(path, b"updated content")
        meta2 = get_metadata(nexus_fs, path)

        assert meta2.created_at == meta1.created_at, "created_at should NOT change"
        assert meta2.modified_at > meta1.modified_at, "modified_at SHOULD change"
        print("✓ Write correctly updates only modified_at")


class TestCacheDoesNotAffectTimestamps:
    """Tests to verify caching doesn't cause timestamp issues."""

    def test_cache_returns_consistent_timestamps(self, nexus_fs):
        """Cached metadata should return same timestamps as uncached."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file
        nexus_fs.write(path, b"test content")

        # First get (might populate cache)
        meta1 = get_metadata(nexus_fs, path)

        # Second get (might use cache)
        meta2 = get_metadata(nexus_fs, path)

        assert meta1.created_at == meta2.created_at
        assert meta1.modified_at == meta2.modified_at
        print("✓ Cached timestamps are consistent")

    def test_timestamps_correct_after_cache_invalidation(self, nexus_fs):
        """Timestamps should be correct after write invalidates cache."""
        path = f"/test/{uuid.uuid4()}/file.txt"

        # Create file and cache metadata
        nexus_fs.write(path, b"v1")
        meta1 = get_metadata(nexus_fs, path)

        time.sleep(0.1)

        # Update file (should invalidate cache)
        nexus_fs.write(path, b"v2")
        meta2 = get_metadata(nexus_fs, path)

        # Verify cache was invalidated and new timestamps are correct
        assert meta2.created_at == meta1.created_at, "created_at should be preserved"
        assert meta2.modified_at > meta1.modified_at, "modified_at should be updated"
        print("✓ Timestamps correct after cache invalidation")


class TestDatabaseDirectAccess:
    """Tests to verify timestamps at database level to rule out any caching issues."""

    def test_read_does_not_modify_database_timestamps(self, nexus_fs):
        """Verify at database level that read operations don't modify timestamps."""
        path = f"/test/{uuid.uuid4()}/db_test.txt"

        # Create file
        nexus_fs.write(path, b"database test content")

        # Clear any cache
        if hasattr(nexus_fs.metadata, "_cache") and nexus_fs.metadata._cache:
            nexus_fs.metadata._cache.clear()

        # Get fresh from DB
        meta1 = nexus_fs.metadata.get(path)
        original_modified = meta1.modified_at
        original_created = meta1.created_at

        time.sleep(0.1)

        # Perform multiple reads
        for _ in range(5):
            nexus_fs.read(path)

        # Clear cache again
        if hasattr(nexus_fs.metadata, "_cache") and nexus_fs.metadata._cache:
            nexus_fs.metadata._cache.clear()

        # Get fresh from DB again
        meta2 = nexus_fs.metadata.get(path)

        assert meta2.created_at == original_created, "created_at changed after reads!"
        assert meta2.modified_at == original_modified, "modified_at changed after reads!"
        print("✓ Database timestamps unchanged after reads (no cache)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
