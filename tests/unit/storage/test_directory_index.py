"""Unit tests for sparse directory index (Issue #924)."""

import tempfile
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


class TestDirectoryIndex:
    """Test suite for sparse directory index functionality."""

    def test_put_creates_directory_entries(self, store):
        """Test that put() creates directory entries for file and all parent dirs."""
        metadata = FileMetadata(
            path="/workspace/src/components/Button.tsx",
            backend_name="local",
            physical_path="/data/abc123",
            size=1024,
            etag="abc123",
            tenant_id="test-tenant",
        )
        store.put(metadata)

        # Check directory entries were created
        # Root level: workspace
        entries = store.list_directory_entries("/", "test-tenant")
        assert entries is not None
        assert len(entries) == 1
        assert entries[0]["name"] == "workspace"
        assert entries[0]["type"] == "directory"

        # Second level: src
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        assert entries is not None
        assert len(entries) == 1
        assert entries[0]["name"] == "src"
        assert entries[0]["type"] == "directory"

        # Third level: components
        entries = store.list_directory_entries("/workspace/src/", "test-tenant")
        assert entries is not None
        assert len(entries) == 1
        assert entries[0]["name"] == "components"
        assert entries[0]["type"] == "directory"

        # Fourth level: Button.tsx (file)
        entries = store.list_directory_entries("/workspace/src/components/", "test-tenant")
        assert entries is not None
        assert len(entries) == 1
        assert entries[0]["name"] == "Button.tsx"
        assert entries[0]["type"] == "file"

    def test_put_multiple_files_same_directory(self, store):
        """Test that multiple files in same directory are all indexed."""
        files = [
            FileMetadata(
                path="/workspace/file1.txt",
                backend_name="local",
                physical_path="/data/1",
                size=100,
                etag="1",
                tenant_id="test-tenant",
            ),
            FileMetadata(
                path="/workspace/file2.txt",
                backend_name="local",
                physical_path="/data/2",
                size=200,
                etag="2",
                tenant_id="test-tenant",
            ),
            FileMetadata(
                path="/workspace/subdir/file3.txt",
                backend_name="local",
                physical_path="/data/3",
                size=300,
                etag="3",
                tenant_id="test-tenant",
            ),
        ]

        for f in files:
            store.put(f)

        # Check /workspace/ has 3 entries: file1.txt, file2.txt, subdir
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        assert entries is not None
        assert len(entries) == 3
        names = {e["name"] for e in entries}
        assert names == {"file1.txt", "file2.txt", "subdir"}

    def test_delete_removes_from_index(self, store):
        """Test that delete() removes file from directory index."""
        metadata = FileMetadata(
            path="/workspace/file.txt",
            backend_name="local",
            physical_path="/data/abc",
            size=100,
            etag="abc",
            tenant_id="test-tenant",
        )
        store.put(metadata)

        # Verify it exists
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        assert entries is not None
        assert len(entries) == 1

        # Delete
        store.delete("/workspace/file.txt")

        # Verify removed from index
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        # Should be None (empty) or empty list - fallback case
        assert entries is None or len(entries) == 0

    def test_put_batch_creates_entries(self, store):
        """Test that put_batch() creates directory entries efficiently."""
        files = [
            FileMetadata(
                path=f"/workspace/batch/file{i}.txt",
                backend_name="local",
                physical_path=f"/data/{i}",
                size=100,
                etag=str(i),
                tenant_id="test-tenant",
            )
            for i in range(10)
        ]

        store.put_batch(files)

        # Check all files are in the index
        entries = store.list_directory_entries("/workspace/batch/", "test-tenant")
        assert entries is not None
        assert len(entries) == 10

    def test_rename_updates_index(self, store):
        """Test that rename_path() updates directory index."""
        metadata = FileMetadata(
            path="/workspace/old.txt",
            backend_name="local",
            physical_path="/data/abc",
            size=100,
            etag="abc",
            tenant_id="test-tenant",
        )
        store.put(metadata)

        # Rename
        store.rename_path("/workspace/old.txt", "/workspace/new.txt")

        # Old should be gone
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        assert entries is not None
        names = [e["name"] for e in entries]
        assert "old.txt" not in names
        assert "new.txt" in names

    def test_fallback_on_empty_index(self, store):
        """Test that list_directory_entries returns None when no index data."""
        # Query a path that has no data
        entries = store.list_directory_entries("/nonexistent/", "test-tenant")
        assert entries is None  # Triggers fallback to LIKE query

    def test_tenant_isolation(self, store):
        """Test that directory entries are isolated by tenant."""
        # Create files for two different tenants
        store.put(
            FileMetadata(
                path="/workspace/file.txt",
                backend_name="local",
                physical_path="/data/1",
                size=100,
                etag="1",
                tenant_id="tenant-a",
            )
        )
        store.put(
            FileMetadata(
                path="/workspace/file.txt",
                backend_name="local",
                physical_path="/data/2",
                size=200,
                etag="2",
                tenant_id="tenant-b",
            )
        )

        # Each tenant should only see their own files
        entries_a = store.list_directory_entries("/workspace/", "tenant-a")
        entries_b = store.list_directory_entries("/workspace/", "tenant-b")

        assert entries_a is not None
        assert entries_b is not None
        assert len(entries_a) == 1
        assert len(entries_b) == 1

    def test_delete_batch_removes_entries(self, store):
        """Test that delete_batch() removes multiple files from index."""
        files = [
            FileMetadata(
                path=f"/workspace/file{i}.txt",
                backend_name="local",
                physical_path=f"/data/{i}",
                size=100,
                etag=str(i),
                tenant_id="test-tenant",
            )
            for i in range(5)
        ]
        store.put_batch(files)

        # Verify all exist
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        assert entries is not None
        assert len(entries) == 5

        # Delete some
        store.delete_batch(["/workspace/file0.txt", "/workspace/file1.txt", "/workspace/file2.txt"])

        # Verify only 2 remain
        entries = store.list_directory_entries("/workspace/", "test-tenant")
        assert entries is not None
        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert names == {"file3.txt", "file4.txt"}
