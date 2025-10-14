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
