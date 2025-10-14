"""Unit tests for metadata store."""

import tempfile
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest

from nexus.core.metadata import FileMetadata
from nexus.core.metadata_sqlite import SQLiteMetadataStore


@pytest.fixture
def temp_db() -> Generator[Path, None, None]:
    """Create a temporary database for tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()
    # Also clean up WAL files if they exist
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    if wal_path.exists():
        wal_path.unlink()
    if shm_path.exists():
        shm_path.unlink()


@pytest.fixture
def metadata_store(temp_db: Path) -> Generator[SQLiteMetadataStore, None, None]:
    """Create a metadata store instance."""
    store = SQLiteMetadataStore(temp_db)
    yield store
    store.close()


def test_init_creates_schema(temp_db: Path) -> None:
    """Test that initialization creates database schema."""
    store = SQLiteMetadataStore(temp_db)

    # Check that tables exist
    cursor = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_paths'"
    )
    assert cursor.fetchone() is not None

    store.close()


def test_put_and_get(metadata_store: SQLiteMetadataStore) -> None:
    """Test storing and retrieving metadata."""
    now = datetime.now()
    metadata = FileMetadata(
        path="/test/file.txt",
        backend_name="local",
        physical_path="test/file.txt",
        size=1234,
        etag="abc123",
        mime_type="text/plain",
        created_at=now,
        modified_at=now,
        version=1,
    )

    # Store metadata
    metadata_store.put(metadata)

    # Retrieve metadata
    result = metadata_store.get("/test/file.txt")

    assert result is not None
    assert result.path == "/test/file.txt"
    assert result.backend_name == "local"
    assert result.physical_path == "test/file.txt"
    assert result.size == 1234
    assert result.etag == "abc123"
    assert result.mime_type == "text/plain"
    assert result.version == 1
    # Datetime comparison (with some tolerance for microseconds)
    assert result.created_at is not None
    assert result.modified_at is not None
    assert abs((result.created_at - now).total_seconds()) < 1
    assert abs((result.modified_at - now).total_seconds()) < 1


def test_get_nonexistent_returns_none(metadata_store: SQLiteMetadataStore) -> None:
    """Test that getting nonexistent metadata returns None."""
    result = metadata_store.get("/nonexistent/file.txt")
    assert result is None


def test_put_update_existing(metadata_store: SQLiteMetadataStore) -> None:
    """Test updating existing metadata."""
    metadata = FileMetadata(
        path="/test/file.txt",
        backend_name="local",
        physical_path="test/file.txt",
        size=1234,
        version=1,
    )

    # Store initial metadata
    metadata_store.put(metadata)

    # Update metadata
    updated_metadata = FileMetadata(
        path="/test/file.txt",
        backend_name="local",
        physical_path="test/file.txt",
        size=5678,
        etag="new-etag",
        version=2,
    )
    metadata_store.put(updated_metadata)

    # Retrieve and verify
    result = metadata_store.get("/test/file.txt")
    assert result is not None
    assert result.size == 5678
    assert result.etag == "new-etag"
    assert result.version == 2


def test_delete(metadata_store: SQLiteMetadataStore) -> None:
    """Test deleting metadata."""
    metadata = FileMetadata(
        path="/test/file.txt",
        backend_name="local",
        physical_path="test/file.txt",
        size=1234,
        version=1,
    )

    # Store metadata
    metadata_store.put(metadata)
    assert metadata_store.exists("/test/file.txt")

    # Delete metadata
    metadata_store.delete("/test/file.txt")
    assert not metadata_store.exists("/test/file.txt")
    assert metadata_store.get("/test/file.txt") is None


def test_exists(metadata_store: SQLiteMetadataStore) -> None:
    """Test checking metadata existence."""
    path = "/test/file.txt"

    # Doesn't exist initially
    assert not metadata_store.exists(path)

    # Create metadata
    metadata = FileMetadata(
        path=path,
        backend_name="local",
        physical_path="test/file.txt",
        size=1234,
        version=1,
    )
    metadata_store.put(metadata)

    # Now exists
    assert metadata_store.exists(path)

    # Delete
    metadata_store.delete(path)

    # Doesn't exist again
    assert not metadata_store.exists(path)


def test_list_all(metadata_store: SQLiteMetadataStore) -> None:
    """Test listing all metadata."""
    # Create multiple files
    files = [
        FileMetadata(
            path="/file1.txt",
            backend_name="local",
            physical_path="file1.txt",
            size=100,
            version=1,
        ),
        FileMetadata(
            path="/dir/file2.txt",
            backend_name="local",
            physical_path="dir/file2.txt",
            size=200,
            version=1,
        ),
        FileMetadata(
            path="/dir/subdir/file3.txt",
            backend_name="local",
            physical_path="dir/subdir/file3.txt",
            size=300,
            version=1,
        ),
    ]

    for metadata in files:
        metadata_store.put(metadata)

    # List all
    results = metadata_store.list()

    assert len(results) == 3
    paths = [r.path for r in results]
    assert "/file1.txt" in paths
    assert "/dir/file2.txt" in paths
    assert "/dir/subdir/file3.txt" in paths


def test_list_with_prefix(metadata_store: SQLiteMetadataStore) -> None:
    """Test listing metadata with prefix filter."""
    # Create multiple files
    files = [
        FileMetadata(
            path="/file1.txt",
            backend_name="local",
            physical_path="file1.txt",
            size=100,
            version=1,
        ),
        FileMetadata(
            path="/dir/file2.txt",
            backend_name="local",
            physical_path="dir/file2.txt",
            size=200,
            version=1,
        ),
        FileMetadata(
            path="/dir/subdir/file3.txt",
            backend_name="local",
            physical_path="dir/subdir/file3.txt",
            size=300,
            version=1,
        ),
        FileMetadata(
            path="/other/file4.txt",
            backend_name="local",
            physical_path="other/file4.txt",
            size=400,
            version=1,
        ),
    ]

    for metadata in files:
        metadata_store.put(metadata)

    # List with prefix "/dir"
    results = metadata_store.list(prefix="/dir")

    assert len(results) == 2
    paths = [r.path for r in results]
    assert "/dir/file2.txt" in paths
    assert "/dir/subdir/file3.txt" in paths
    assert "/file1.txt" not in paths
    assert "/other/file4.txt" not in paths


def test_list_empty(metadata_store: SQLiteMetadataStore) -> None:
    """Test listing when no files exist."""
    results = metadata_store.list()
    assert len(results) == 0


def test_context_manager(temp_db: Path) -> None:
    """Test using metadata store as context manager."""
    with SQLiteMetadataStore(temp_db) as store:
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="test.txt",
            size=100,
            version=1,
        )
        store.put(metadata)

        result = store.get("/test.txt")
        assert result is not None


def test_metadata_with_optional_fields(metadata_store: SQLiteMetadataStore) -> None:
    """Test metadata with only required fields."""
    metadata = FileMetadata(
        path="/minimal.txt",
        backend_name="local",
        physical_path="minimal.txt",
        size=100,
        version=1,
        # Optional fields left as None
        etag=None,
        mime_type=None,
        created_at=None,
        modified_at=None,
    )

    metadata_store.put(metadata)

    result = metadata_store.get("/minimal.txt")
    assert result is not None
    assert result.path == "/minimal.txt"
    assert result.etag is None
    assert result.mime_type is None
    assert result.created_at is None
    assert result.modified_at is None


def test_concurrent_operations(metadata_store: SQLiteMetadataStore) -> None:
    """Test that WAL mode allows concurrent reads."""
    # Store some metadata
    metadata = FileMetadata(
        path="/test.txt",
        backend_name="local",
        physical_path="test.txt",
        size=100,
        version=1,
    )
    metadata_store.put(metadata)

    # Verify WAL mode is enabled
    cursor = metadata_store.conn.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    assert mode.lower() == "wal"
