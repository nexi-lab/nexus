"""Unit tests for SQLAlchemy-based metadata store."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.core.metadata import FileMetadata
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def store(temp_db):
    """Create a metadata store instance."""
    store = SQLAlchemyMetadataStore(temp_db)
    yield store
    store.close()


class TestSQLAlchemyMetadataStore:
    """Test suite for SQLAlchemyMetadataStore."""

    def test_init_creates_database(self, temp_db):
        """Test that initialization creates database file."""
        store = SQLAlchemyMetadataStore(temp_db)
        assert temp_db.exists()
        store.close()

    def test_put_and_get(self, store):
        """Test storing and retrieving file metadata."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="abc123",
            mime_type="text/plain",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )

        store.put(metadata)
        retrieved = store.get("/test/file.txt")

        assert retrieved is not None
        assert retrieved.path == metadata.path
        assert retrieved.backend_name == metadata.backend_name
        assert retrieved.physical_path == metadata.physical_path
        assert retrieved.size == metadata.size
        assert retrieved.etag == metadata.etag
        assert retrieved.mime_type == metadata.mime_type

    def test_get_nonexistent(self, store):
        """Test getting metadata for nonexistent file."""
        result = store.get("/nonexistent/file.txt")
        assert result is None

    def test_update_existing(self, store):
        """Test updating existing file metadata."""
        # Create initial metadata
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="abc123",
            mime_type="text/plain",
        )
        store.put(metadata)

        # Update with new values
        updated_metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="s3",
            physical_path="/bucket/file.txt",
            size=2048,
            etag="def456",
            mime_type="text/plain",
        )
        store.put(updated_metadata)

        # Verify update
        retrieved = store.get("/test/file.txt")
        assert retrieved is not None
        assert retrieved.backend_name == "s3"
        assert retrieved.physical_path == "/bucket/file.txt"
        assert retrieved.size == 2048
        assert retrieved.etag == "def456"

    def test_exists(self, store):
        """Test checking file existence."""
        assert not store.exists("/test/file.txt")

        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store.put(metadata)

        assert store.exists("/test/file.txt")

    def test_delete(self, store):
        """Test deleting file metadata."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store.put(metadata)

        assert store.exists("/test/file.txt")
        store.delete("/test/file.txt")
        assert not store.exists("/test/file.txt")

        # Should return None after deletion
        assert store.get("/test/file.txt") is None

    def test_list_empty(self, store):
        """Test listing files when store is empty."""
        result = store.list()
        assert result == []

    def test_list_all(self, store):
        """Test listing all files."""
        files = [
            FileMetadata(
                path="/file1.txt", backend_name="local", physical_path="/data/1", size=100
            ),
            FileMetadata(
                path="/file2.txt", backend_name="local", physical_path="/data/2", size=200
            ),
            FileMetadata(
                path="/dir/file3.txt", backend_name="local", physical_path="/data/3", size=300
            ),
        ]

        for f in files:
            store.put(f)

        result = store.list()
        assert len(result) == 3
        assert all(isinstance(m, FileMetadata) for m in result)

    def test_list_with_prefix(self, store):
        """Test listing files with path prefix."""
        files = [
            FileMetadata(
                path="/dir1/file1.txt", backend_name="local", physical_path="/data/1", size=100
            ),
            FileMetadata(
                path="/dir1/file2.txt", backend_name="local", physical_path="/data/2", size=200
            ),
            FileMetadata(
                path="/dir2/file3.txt", backend_name="local", physical_path="/data/3", size=300
            ),
        ]

        for f in files:
            store.put(f)

        result = store.list(prefix="/dir1")
        assert len(result) == 2
        assert all(m.path.startswith("/dir1") for m in result)

    def test_list_sorted(self, store):
        """Test that list returns results sorted by path."""
        files = [
            FileMetadata(path="/z.txt", backend_name="local", physical_path="/data/z", size=100),
            FileMetadata(path="/a.txt", backend_name="local", physical_path="/data/a", size=200),
            FileMetadata(path="/m.txt", backend_name="local", physical_path="/data/m", size=300),
        ]

        for f in files:
            store.put(f)

        result = store.list()
        paths = [m.path for m in result]
        assert paths == ["/a.txt", "/m.txt", "/z.txt"]

    def test_context_manager(self, temp_db):
        """Test using store as context manager."""
        with SQLAlchemyMetadataStore(temp_db) as store:
            metadata = FileMetadata(
                path="/test/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=1024,
            )
            store.put(metadata)
            assert store.exists("/test/file.txt")

    def test_get_file_metadata(self, store):
        """Test getting file metadata key-value pairs."""
        # First create a file
        file_metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store.put(file_metadata)

        # Set metadata
        store.set_file_metadata("/test/file.txt", "author", "John Doe")
        store.set_file_metadata("/test/file.txt", "tags", ["python", "test"])

        # Get metadata
        author = store.get_file_metadata("/test/file.txt", "author")
        tags = store.get_file_metadata("/test/file.txt", "tags")

        assert author == "John Doe"
        assert tags == ["python", "test"]

    def test_get_nonexistent_file_metadata(self, store):
        """Test getting metadata for nonexistent file."""
        result = store.get_file_metadata("/nonexistent/file.txt", "key")
        assert result is None

    def test_update_file_metadata(self, store):
        """Test updating file metadata key-value pairs."""
        # Create file
        file_metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store.put(file_metadata)

        # Set initial value
        store.set_file_metadata("/test/file.txt", "version", 1)
        assert store.get_file_metadata("/test/file.txt", "version") == 1

        # Update value
        store.set_file_metadata("/test/file.txt", "version", 2)
        assert store.get_file_metadata("/test/file.txt", "version") == 2

    def test_concurrent_access(self, temp_db):
        """Test concurrent access with multiple store instances."""
        # Create first store and add data
        store1 = SQLAlchemyMetadataStore(temp_db)
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store1.put(metadata)

        # Create second store and verify it can read the data
        store2 = SQLAlchemyMetadataStore(temp_db)
        retrieved = store2.get("/test/file.txt")

        assert retrieved is not None
        assert retrieved.path == metadata.path

        store1.close()
        store2.close()


class TestSQLAlchemyMetadataStoreModels:
    """Test SQLAlchemy models integration."""

    def test_soft_delete_does_not_appear_in_list(self, store):
        """Test that soft-deleted files don't appear in list."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store.put(metadata)

        assert len(store.list()) == 1
        store.delete("/test/file.txt")
        assert len(store.list()) == 0

    def test_unique_constraint_virtual_path(self, store):
        """Test that virtual path is unique per tenant."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
        )
        store.put(metadata)

        # Putting same path should update, not create duplicate
        metadata2 = FileMetadata(
            path="/test/file.txt",
            backend_name="s3",
            physical_path="/bucket/file.txt",
            size=2048,
        )
        store.put(metadata2)

        # Should only have one file
        results = store.list()
        assert len(results) == 1
        assert results[0].backend_name == "s3"


class TestVersionTracking:
    """Test version tracking functionality (v0.3.5)."""

    def test_version_tracking_on_new_file(self, store):
        """Test that new files start with version 1."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
        )
        store.put(metadata)

        retrieved = store.get("/test/file.txt")
        assert retrieved.version == 1

    def test_version_increment_on_update(self, store):
        """Test that updating a file increments its version."""
        # Create initial file
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
        )
        store.put(metadata)

        # Update file
        metadata_v2 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=2048,
            etag="hash2",
        )
        store.put(metadata_v2)

        retrieved = store.get("/test/file.txt")
        assert retrieved.version == 2

    def test_get_specific_version(self, store):
        """Test retrieving a specific version of a file."""
        # Create initial file
        metadata_v1 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
        )
        store.put(metadata_v1)

        # Update to version 2
        metadata_v2 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=2048,
            etag="hash2",
        )
        store.put(metadata_v2)

        # Get version 1
        v1 = store.get_version("/test/file.txt", version=1)
        assert v1 is not None
        assert v1.version == 1
        assert v1.etag == "hash1"
        assert v1.size == 1024

        # Get version 2
        v2 = store.get_version("/test/file.txt", version=2)
        assert v2 is not None
        assert v2.version == 2
        assert v2.etag == "hash2"
        assert v2.size == 2048

    def test_get_nonexistent_version(self, store):
        """Test getting a version that doesn't exist."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
        )
        store.put(metadata)

        # Try to get version 999
        result = store.get_version("/test/file.txt", version=999)
        assert result is None

    def test_list_versions(self, store):
        """Test listing all versions of a file."""
        # Create initial file
        metadata_v1 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
        )
        store.put(metadata_v1)

        # Update multiple times
        for i in range(2, 5):
            metadata = FileMetadata(
                path="/test/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=1024 * i,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # List versions
        versions = store.list_versions("/test/file.txt")
        assert len(versions) == 4

        # Should be sorted by version descending (newest first)
        assert versions[0]["version"] == 4
        assert versions[1]["version"] == 3
        assert versions[2]["version"] == 2
        assert versions[3]["version"] == 1

        # Verify content hashes
        assert versions[0]["content_hash"] == "hash4"
        assert versions[3]["content_hash"] == "hash1"

    def test_rollback_to_previous_version(self, store):
        """Test rolling back a file to a previous version."""
        # Create initial file (v1)
        metadata_v1 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
        )
        store.put(metadata_v1)

        # Update to v2
        metadata_v2 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=2048,
            etag="hash2",
        )
        store.put(metadata_v2)

        # Update to v3
        metadata_v3 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=3072,
            etag="hash3",
        )
        store.put(metadata_v3)

        # Rollback to v1
        store.rollback("/test/file.txt", version=1)

        # Current file should now have v1's content but be v4
        current = store.get("/test/file.txt")
        assert current.version == 4
        assert current.etag == "hash1"
        assert current.size == 1024

        # Verify version history
        versions = store.list_versions("/test/file.txt")
        assert len(versions) == 4
        assert versions[0]["version"] == 4
        assert versions[0]["source_type"] == "rollback"
        assert "Rollback to version 1" in versions[0]["change_reason"]

    def test_get_version_diff(self, store):
        """Test getting diff between two versions."""
        # Create v1
        metadata_v1 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=1024,
            etag="hash1",
            mime_type="text/plain",
        )
        store.put(metadata_v1)

        # Create v2
        metadata_v2 = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=2048,
            etag="hash2",
            mime_type="text/plain",
        )
        store.put(metadata_v2)

        # Get diff
        diff = store.get_version_diff("/test/file.txt", v1=1, v2=2)

        assert diff["path"] == "/test/file.txt"
        assert diff["v1"] == 1
        assert diff["v2"] == 2
        assert diff["content_hash_v1"] == "hash1"
        assert diff["content_hash_v2"] == "hash2"
        assert diff["content_changed"] is True
        assert diff["size_v1"] == 1024
        assert diff["size_v2"] == 2048
        assert diff["size_delta"] == 1024

    def test_list_versions_nonexistent_file(self, store):
        """Test listing versions for a file that doesn't exist."""
        versions = store.list_versions("/nonexistent.txt")
        assert versions == []


class TestBatchOperations:
    """Test batch operations for performance."""

    def test_get_batch(self, store):
        """Test getting multiple files in a single query."""
        # Create multiple files
        files = [
            FileMetadata(
                path=f"/file{i}.txt",
                backend_name="local",
                physical_path=f"/data/{i}",
                size=100 * i,
                etag=f"hash{i}",
            )
            for i in range(1, 6)
        ]
        for f in files:
            store.put(f)

        # Batch get
        paths = [f"/file{i}.txt" for i in range(1, 6)] + ["/nonexistent.txt"]
        result = store.get_batch(paths)

        assert len(result) == 6
        assert result["/file1.txt"].size == 100
        assert result["/file5.txt"].size == 500
        assert result["/nonexistent.txt"] is None

    def test_get_batch_empty(self, store):
        """Test batch get with empty list."""
        result = store.get_batch([])
        assert result == {}

    def test_delete_batch(self, store):
        """Test deleting multiple files in a single transaction."""
        # Create multiple files
        files = [
            FileMetadata(
                path=f"/file{i}.txt", backend_name="local", physical_path=f"/data/{i}", size=100
            )
            for i in range(1, 6)
        ]
        for f in files:
            store.put(f)

        # Delete batch
        store.delete_batch(["/file1.txt", "/file3.txt", "/file5.txt"])

        # Verify deletions
        assert not store.exists("/file1.txt")
        assert store.exists("/file2.txt")
        assert not store.exists("/file3.txt")
        assert store.exists("/file4.txt")
        assert not store.exists("/file5.txt")

    def test_delete_batch_empty(self, store):
        """Test batch delete with empty list."""
        store.delete_batch([])  # Should not raise

    def test_put_batch(self, store):
        """Test storing multiple files in a single transaction."""
        files = [
            FileMetadata(
                path=f"/file{i}.txt", backend_name="local", physical_path=f"/data/{i}", size=100 * i
            )
            for i in range(1, 6)
        ]

        store.put_batch(files)

        # Verify all files were stored
        for f in files:
            assert store.exists(f.path)

    def test_batch_get_content_ids(self, store):
        """Test getting content IDs for multiple files."""
        # Create files
        files = [
            FileMetadata(
                path=f"/file{i}.txt",
                backend_name="local",
                physical_path=f"/data/{i}",
                size=100,
                etag=f"hash{i}",
            )
            for i in range(1, 4)
        ]
        for f in files:
            store.put(f)

        # Get content IDs
        paths = ["/file1.txt", "/file2.txt", "/file3.txt", "/nonexistent.txt"]
        result = store.batch_get_content_ids(paths)

        assert len(result) == 4
        assert result["/file1.txt"] == "hash1"
        assert result["/file2.txt"] == "hash2"
        assert result["/file3.txt"] == "hash3"
        assert result["/nonexistent.txt"] is None


@pytest.mark.skip(
    reason="v0.5.0: Work detection queries use deprecated tenant_id column - use ReBAC instead"
)
class TestWorkDetectionQueries:
    """Test SQL views for work detection."""

    def test_get_ready_work(self, store):
        """Test getting ready work items."""
        # Create a file with work status
        metadata = FileMetadata(
            path="/work/task1.txt",
            backend_name="local",
            physical_path="/data/task1",
            size=1024,
        )
        store.put(metadata)

        # Set work status to ready
        store.set_file_metadata("/work/task1.txt", "status", "ready")
        store.set_file_metadata("/work/task1.txt", "priority", 10)

        # Get ready work
        work_items = store.get_ready_work()
        assert isinstance(work_items, list)

    def test_get_pending_work(self, store):
        """Test getting pending work items."""
        metadata = FileMetadata(
            path="/work/task1.txt",
            backend_name="local",
            physical_path="/data/task1",
            size=1024,
        )
        store.put(metadata)

        store.set_file_metadata("/work/task1.txt", "status", "pending")
        store.set_file_metadata("/work/task1.txt", "priority", 5)

        work_items = store.get_pending_work(limit=10)
        assert isinstance(work_items, list)

    def test_get_blocked_work(self, store):
        """Test getting blocked work items."""
        work_items = store.get_blocked_work(limit=5)
        assert isinstance(work_items, list)

    def test_get_in_progress_work(self, store):
        """Test getting in-progress work items."""
        work_items = store.get_in_progress_work()
        assert isinstance(work_items, list)

    def test_get_work_by_priority(self, store):
        """Test getting work items ordered by priority."""
        work_items = store.get_work_by_priority(limit=10)
        assert isinstance(work_items, list)


class TestAdditionalMethods:
    """Test additional metadata store methods."""

    def test_rename_path(self, store):
        """Test renaming/moving a file."""
        metadata = FileMetadata(
            path="/old/path.txt",
            backend_name="local",
            physical_path="/data/file",
            size=1024,
            etag="hash1",
        )
        store.put(metadata)

        # Rename
        store.rename_path("/old/path.txt", "/new/path.txt")

        # Old path should not exist
        assert not store.exists("/old/path.txt")

        # New path should exist with same metadata
        retrieved = store.get("/new/path.txt")
        assert retrieved is not None
        assert retrieved.physical_path == "/data/file"
        assert retrieved.etag == "hash1"

    def test_is_implicit_directory(self, store):
        """Test checking if a path is an implicit directory."""
        # Create files under a directory
        files = [
            FileMetadata(
                path="/dir/file1.txt", backend_name="local", physical_path="/data/1", size=100
            ),
            FileMetadata(
                path="/dir/file2.txt", backend_name="local", physical_path="/data/2", size=200
            ),
        ]
        for f in files:
            store.put(f)

        # /dir should be an implicit directory
        assert store.is_implicit_directory("/dir")

        # /dir/file1.txt is not a directory
        assert not store.is_implicit_directory("/dir/file1.txt")

        # /nonexistent is not a directory
        assert not store.is_implicit_directory("/nonexistent")

    def test_get_path_id(self, store):
        """Test getting path_id for a file."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file",
            size=1024,
        )
        store.put(metadata)

        path_id = store.get_path_id("/test/file.txt")
        assert path_id is not None
        assert isinstance(path_id, str)

        # Nonexistent file should return None
        assert store.get_path_id("/nonexistent.txt") is None

    def test_cache_stats(self, store):
        """Test getting cache statistics."""
        stats = store.get_cache_stats()
        assert stats is not None
        assert isinstance(stats, dict)

    def test_clear_cache(self, store):
        """Test clearing the cache."""
        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file",
            size=1024,
        )
        store.put(metadata)

        # Access to populate cache
        store.get("/test/file.txt")
        store.exists("/test/file.txt")

        # Clear cache
        store.clear_cache()

        # File should still be accessible (from DB)
        assert store.get("/test/file.txt") is not None


class TestErrorHandling:
    """Test error handling in metadata store."""

    def test_init_without_db_path_or_url(self):
        """Test initialization without database path or URL."""
        from nexus.core.exceptions import MetadataError

        with pytest.raises(MetadataError, match="Database URL must be provided"):
            SQLAlchemyMetadataStore(db_path=None, db_url=None)

    def test_set_file_metadata_nonexistent_file(self, store):
        """Test setting metadata on a nonexistent file."""
        from nexus.core.exceptions import MetadataError

        with pytest.raises(MetadataError, match="File not found"):
            store.set_file_metadata("/nonexistent.txt", "key", "value")

    def test_rename_nonexistent_source(self, store):
        """Test renaming a file that doesn't exist."""
        from nexus.core.exceptions import MetadataError

        with pytest.raises(MetadataError, match="Source path not found"):
            store.rename_path("/nonexistent.txt", "/new.txt")

    def test_rename_to_existing_destination(self, store):
        """Test renaming to a path that already exists."""
        from nexus.core.exceptions import MetadataError

        # Create two files
        metadata1 = FileMetadata(
            path="/file1.txt",
            backend_name="local",
            physical_path="/data/1",
            size=1024,
        )
        metadata2 = FileMetadata(
            path="/file2.txt",
            backend_name="local",
            physical_path="/data/2",
            size=2048,
        )
        store.put(metadata1)
        store.put(metadata2)

        # Try to rename file1 to file2 (should fail)
        with pytest.raises(MetadataError, match="Destination path already exists"):
            store.rename_path("/file1.txt", "/file2.txt")

    def test_rollback_nonexistent_file(self, store):
        """Test rollback on a nonexistent file."""
        from nexus.core.exceptions import MetadataError

        with pytest.raises(MetadataError, match="File not found"):
            store.rollback("/nonexistent.txt", version=1)

    def test_rollback_nonexistent_version(self, store):
        """Test rollback to a nonexistent version."""
        from nexus.core.exceptions import MetadataError

        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file",
            size=1024,
            etag="hash1",
        )
        store.put(metadata)

        with pytest.raises(MetadataError, match="Version .* not found"):
            store.rollback("/test/file.txt", version=999)

    def test_version_diff_nonexistent_file(self, store):
        """Test version diff on nonexistent file."""
        from nexus.core.exceptions import MetadataError

        with pytest.raises(MetadataError, match="File not found"):
            store.get_version_diff("/nonexistent.txt", v1=1, v2=2)

    def test_version_diff_nonexistent_versions(self, store):
        """Test version diff with nonexistent versions."""
        from nexus.core.exceptions import MetadataError

        metadata = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file",
            size=1024,
            etag="hash1",
        )
        store.put(metadata)

        with pytest.raises(MetadataError, match="Version .* not found"):
            store.get_version_diff("/test/file.txt", v1=1, v2=999)
