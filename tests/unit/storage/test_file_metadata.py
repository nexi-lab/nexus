"""Unit tests for custom file metadata (key-value) functionality.

Tests the RaftMetadataStore's ability to store and retrieve custom
key-value metadata on files (set, get, delete, list operations).

Requires: Built Rust extensions and generated protobuf files.
Skip these tests in environments without full build (e.g., lint-only CI).
"""

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Skip entire module if required build artifacts aren't available
pytest.importorskip("nexus.core._metadata_generated", reason="Requires built Rust extensions")
pytest.importorskip("nexus.core.metadata_pb2", reason="Requires generated protobuf files")

from nexus.core.metadata import FileMetadata
from nexus.storage.raft_metadata_store import RaftMetadataStore


@pytest.fixture
def temp_db():
    """Create a temporary database directory for testing."""
    db_path = Path(tempfile.mkdtemp(prefix="nexus_test_"))
    yield db_path
    # Cleanup
    if db_path.exists():
        shutil.rmtree(db_path)


@pytest.fixture
def store(temp_db):
    """Create a RaftMetadataStore instance."""
    store = RaftMetadataStore.local(str(temp_db))
    yield store
    # RaftMetadataStore doesn't have a close method, cleanup is handled by temp_db fixture


@pytest.fixture
def sample_file(store):
    """Create a sample file for testing metadata operations."""
    metadata = FileMetadata(
        path="/test/sample.txt",
        backend_name="local",
        physical_path="/data/sample.txt",
        size=1024,
        etag="abc123",
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )
    store.put(metadata)
    return "/test/sample.txt"


class TestFileMetadata:
    """Test suite for custom file metadata operations."""

    def test_set_and_get_string_metadata(self, store, sample_file):
        """Test setting and getting string metadata."""
        store.set_file_metadata(sample_file, "author", "Alice")
        value = store.get_file_metadata(sample_file, "author")
        assert value == "Alice"

    def test_set_and_get_numeric_metadata(self, store, sample_file):
        """Test setting and getting numeric metadata."""
        store.set_file_metadata(sample_file, "version", 42)
        value = store.get_file_metadata(sample_file, "version")
        assert value == 42

    def test_set_and_get_boolean_metadata(self, store, sample_file):
        """Test setting and getting boolean metadata."""
        store.set_file_metadata(sample_file, "reviewed", True)
        value = store.get_file_metadata(sample_file, "reviewed")
        assert value is True

    def test_set_and_get_list_metadata(self, store, sample_file):
        """Test setting and getting list metadata."""
        tags = ["important", "draft", "v2"]
        store.set_file_metadata(sample_file, "tags", tags)
        value = store.get_file_metadata(sample_file, "tags")
        assert value == tags

    def test_set_and_get_dict_metadata(self, store, sample_file):
        """Test setting and getting dict metadata."""
        metadata = {"author": "Alice", "department": "Engineering", "priority": 1}
        store.set_file_metadata(sample_file, "info", metadata)
        value = store.get_file_metadata(sample_file, "info")
        assert value == metadata

    def test_set_and_get_none_metadata(self, store, sample_file):
        """Test setting None deletes the metadata (RaftMetadataStore behavior)."""
        # First set a value
        store.set_file_metadata(sample_file, "nullable", "some_value")
        assert store.get_file_metadata(sample_file, "nullable") == "some_value"

        # Setting None should delete the key (RaftMetadataStore behavior)
        store.set_file_metadata(sample_file, "nullable", None)
        value = store.get_file_metadata(sample_file, "nullable")
        assert value is None

    def test_get_nonexistent_metadata(self, store, sample_file):
        """Test getting metadata key that doesn't exist."""
        value = store.get_file_metadata(sample_file, "nonexistent")
        assert value is None

    def test_update_metadata(self, store, sample_file):
        """Test updating existing metadata value."""
        store.set_file_metadata(sample_file, "status", "draft")
        store.set_file_metadata(sample_file, "status", "published")
        value = store.get_file_metadata(sample_file, "status")
        assert value == "published"

    def test_delete_metadata(self, store, sample_file):
        """Test deleting metadata key."""
        store.set_file_metadata(sample_file, "temporary", "value")
        assert store.get_file_metadata(sample_file, "temporary") == "value"

        deleted = store.delete_file_metadata(sample_file, "temporary")
        assert deleted is True
        assert store.get_file_metadata(sample_file, "temporary") is None

    def test_delete_nonexistent_metadata(self, store, sample_file):
        """Test deleting metadata key that doesn't exist."""
        deleted = store.delete_file_metadata(sample_file, "nonexistent")
        assert deleted is False

    def test_get_all_file_metadata_empty(self, store, sample_file):
        """Test getting all metadata when none is set."""
        metadata = store.get_all_file_metadata(sample_file)
        assert metadata == {}

    def test_get_all_file_metadata(self, store, sample_file):
        """Test getting all metadata for a file."""
        store.set_file_metadata(sample_file, "author", "Alice")
        store.set_file_metadata(sample_file, "version", 2)
        store.set_file_metadata(sample_file, "tags", ["important"])

        metadata = store.get_all_file_metadata(sample_file)
        assert metadata == {
            "author": "Alice",
            "version": 2,
            "tags": ["important"],
        }

    def test_multiple_files_metadata(self, store):
        """Test metadata is isolated between files."""
        # Create two files
        for path in ["/test/file1.txt", "/test/file2.txt"]:
            metadata = FileMetadata(
                path=path,
                backend_name="local",
                physical_path=f"/data/{path}",
                size=100,
                etag="hash",
                mime_type="text/plain",
            )
            store.put(metadata)

        # Set different metadata on each
        store.set_file_metadata("/test/file1.txt", "owner", "Alice")
        store.set_file_metadata("/test/file2.txt", "owner", "Bob")

        # Verify isolation
        assert store.get_file_metadata("/test/file1.txt", "owner") == "Alice"
        assert store.get_file_metadata("/test/file2.txt", "owner") == "Bob"

    def test_bulk_get_metadata(self, store):
        """Test bulk getting metadata for multiple files."""
        # Create multiple files with same metadata key
        paths = []
        for i in range(5):
            path = f"/test/file{i}.txt"
            metadata = FileMetadata(
                path=path,
                backend_name="local",
                physical_path=f"/data/file{i}.txt",
                size=100,
                etag="hash",
                mime_type="text/plain",
            )
            store.put(metadata)
            paths.append(path)

        # Set priority metadata on some files
        store.set_file_metadata("/test/file0.txt", "priority", "high")
        store.set_file_metadata("/test/file2.txt", "priority", "medium")
        store.set_file_metadata("/test/file4.txt", "priority", "low")

        # Bulk get
        results = store.get_file_metadata_bulk(paths, "priority")
        assert results["/test/file0.txt"] == "high"
        assert results["/test/file2.txt"] == "medium"
        assert results["/test/file4.txt"] == "low"
        # Files without the key return None
        assert results["/test/file1.txt"] is None
        assert results["/test/file3.txt"] is None

    def test_metadata_persists_after_file_update(self, store, sample_file):
        """Test that metadata survives file content updates."""
        # Set metadata
        store.set_file_metadata(sample_file, "category", "documents")

        # Update file (simulating content change)
        updated_metadata = FileMetadata(
            path=sample_file,
            backend_name="local",
            physical_path="/data/sample_v2.txt",
            size=2048,
            etag="newhash",
            mime_type="text/plain",
        )
        store.put(updated_metadata)

        # Metadata should still exist (stored separately from file metadata)
        value = store.get_file_metadata(sample_file, "category")
        assert value == "documents"

    def test_metadata_key_isolation(self, store, sample_file):
        """Test that different keys on same file are isolated."""
        store.set_file_metadata(sample_file, "key1", "value1")
        store.set_file_metadata(sample_file, "key2", "value2")

        # Deleting one key shouldn't affect the other
        store.delete_file_metadata(sample_file, "key1")

        assert store.get_file_metadata(sample_file, "key1") is None
        assert store.get_file_metadata(sample_file, "key2") == "value2"

    def test_complex_nested_metadata(self, store, sample_file):
        """Test storing complex nested structures."""
        complex_data = {
            "workflow": {
                "status": "running",
                "steps": [
                    {"name": "extract", "completed": True},
                    {"name": "transform", "completed": False},
                ],
                "metadata": {"retry_count": 3, "timeout": 300},
            }
        }
        store.set_file_metadata(sample_file, "workflow_state", complex_data)
        result = store.get_file_metadata(sample_file, "workflow_state")
        assert result == complex_data
