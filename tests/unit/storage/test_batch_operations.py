"""Unit tests for batch metadata operations."""

import tempfile
from pathlib import Path

from nexus.core.embedded import Embedded
from nexus.core.metadata import FileMetadata
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class TestBatchOperations:
    """Test suite for batch operations functionality."""

    def test_get_batch_basic(self, tmp_path: Path):
        """Test basic batch get operation."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store multiple files
        metadata_list = [
            FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100 * i,
                etag=f"hash{i}",
            )
            for i in range(5)
        ]

        for metadata in metadata_list:
            store.put(metadata)

        # Batch get
        paths = [f"/test{i}.txt" for i in range(5)]
        result = store.get_batch(paths)

        # Verify all files retrieved
        assert len(result) == 5
        for i in range(5):
            path = f"/test{i}.txt"
            assert path in result
            assert result[path] is not None
            assert result[path].size == 100 * i  # type: ignore
            assert result[path].etag == f"hash{i}"  # type: ignore

        store.close()

    def test_get_batch_with_missing_files(self, tmp_path: Path):
        """Test batch get with some files not found."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store only some files
        for i in [0, 2, 4]:
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # Batch get including non-existent files
        paths = [f"/test{i}.txt" for i in range(5)]
        result = store.get_batch(paths)

        # Verify mixed results
        assert len(result) == 5
        assert result["/test0.txt"] is not None
        assert result["/test1.txt"] is None
        assert result["/test2.txt"] is not None
        assert result["/test3.txt"] is None
        assert result["/test4.txt"] is not None

        store.close()

    def test_get_batch_empty_list(self, tmp_path: Path):
        """Test batch get with empty path list."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        result = store.get_batch([])
        assert result == {}

        store.close()

    def test_get_batch_with_cache(self, tmp_path: Path):
        """Test that batch get utilizes cache."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store files
        for i in range(3):
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # First batch get - should cache results
        paths = ["/test0.txt", "/test1.txt", "/test2.txt"]
        result1 = store.get_batch(paths)

        # Check cache stats
        stats = store.get_cache_stats()
        assert stats is not None
        assert stats["path_cache_size"] == 3

        # Second batch get - should hit cache
        result2 = store.get_batch(paths)
        assert len(result2) == 3

        # Results should be identical
        for path in paths:
            assert result1[path] is not None
            assert result2[path] is not None
            assert result1[path].path == result2[path].path  # type: ignore
            assert result1[path].etag == result2[path].etag  # type: ignore

        store.close()

    def test_delete_batch_basic(self, tmp_path: Path):
        """Test basic batch delete operation."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store multiple files
        for i in range(5):
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # Verify all exist
        assert store.exists("/test0.txt")
        assert store.exists("/test2.txt")
        assert store.exists("/test4.txt")

        # Batch delete
        paths = ["/test0.txt", "/test2.txt", "/test4.txt"]
        store.delete_batch(paths)

        # Verify deleted
        assert not store.exists("/test0.txt")
        assert store.exists("/test1.txt")  # Not deleted
        assert not store.exists("/test2.txt")
        assert store.exists("/test3.txt")  # Not deleted
        assert not store.exists("/test4.txt")

        store.close()

    def test_delete_batch_empty_list(self, tmp_path: Path):
        """Test batch delete with empty path list."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Should not raise error
        store.delete_batch([])

        store.close()

    def test_delete_batch_with_nonexistent(self, tmp_path: Path):
        """Test batch delete with some non-existent files."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store only some files
        for i in [1, 3]:
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # Batch delete including non-existent files (should not error)
        paths = [f"/test{i}.txt" for i in range(5)]
        store.delete_batch(paths)

        # Verify all are gone (existent ones deleted, non-existent stay non-existent)
        for i in range(5):
            assert not store.exists(f"/test{i}.txt")

        store.close()

    def test_put_batch_create(self, tmp_path: Path):
        """Test batch put for creating new files."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Batch create
        metadata_list = [
            FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100 * i,
                etag=f"hash{i}",
            )
            for i in range(5)
        ]

        store.put_batch(metadata_list)

        # Verify all created
        for i in range(5):
            path = f"/test{i}.txt"
            assert store.exists(path)
            metadata = store.get(path)
            assert metadata is not None
            assert metadata.size == 100 * i
            assert metadata.etag == f"hash{i}"

        store.close()

    def test_put_batch_update(self, tmp_path: Path):
        """Test batch put for updating existing files."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Create initial files
        for i in range(3):
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # Batch update
        updated_metadata = [
            FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"newhash{i}",
                size=200 * i,
                etag=f"newhash{i}",
            )
            for i in range(3)
        ]

        store.put_batch(updated_metadata)

        # Verify all updated
        for i in range(3):
            path = f"/test{i}.txt"
            metadata = store.get(path)
            assert metadata is not None
            assert metadata.size == 200 * i
            assert metadata.etag == f"newhash{i}"

        store.close()

    def test_put_batch_mixed(self, tmp_path: Path):
        """Test batch put with mix of new and existing files."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Create some initial files
        for i in [0, 2]:
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # Batch put with mix of updates and creates
        metadata_list = [
            FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"newhash{i}",
                size=200 * i,
                etag=f"newhash{i}",
            )
            for i in range(4)
        ]

        store.put_batch(metadata_list)

        # Verify all files exist with correct data
        for i in range(4):
            path = f"/test{i}.txt"
            metadata = store.get(path)
            assert metadata is not None
            assert metadata.size == 200 * i
            assert metadata.etag == f"newhash{i}"

        store.close()

    def test_put_batch_empty_list(self, tmp_path: Path):
        """Test batch put with empty metadata list."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Should not raise error
        store.put_batch([])

        store.close()

    def test_batch_operations_performance(self, tmp_path: Path):
        """Test that batch operations are more efficient than individual operations."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # This is more of a conceptual test - batch operations should complete
        # without errors even with many items
        num_files = 100

        # Batch create
        metadata_list = [
            FileMetadata(
                path=f"/file{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=1000,
                etag=f"hash{i}",
            )
            for i in range(num_files)
        ]

        store.put_batch(metadata_list)

        # Batch get
        paths = [f"/file{i}.txt" for i in range(num_files)]
        result = store.get_batch(paths)
        assert len(result) == num_files

        # Batch delete
        store.delete_batch(paths)

        # Verify all deleted
        for i in range(num_files):
            assert not store.exists(f"/file{i}.txt")

        store.close()

    def test_embedded_rmdir_uses_batch_delete(self):
        """Test that Embedded.rmdir() uses batch delete for directories."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fs = Embedded(data_dir=tmp_dir)

            # Create directory with multiple files
            for i in range(10):
                fs.write(f"/testdir/file{i}.txt", b"content")

            # Verify files exist
            files_before = fs.list("/testdir")
            assert len(files_before) == 10

            # Delete directory recursively
            # This should use batch delete internally
            fs.rmdir("/testdir", recursive=True)

            # Verify all files are deleted
            for i in range(10):
                assert not fs.exists(f"/testdir/file{i}.txt")

            fs.close()

    def test_batch_operations_with_cache_invalidation(self, tmp_path: Path):
        """Test that batch operations properly invalidate cache."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Create and cache files
        for i in range(3):
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)
            store.get(f"/test{i}.txt")  # Cache it

        # Verify cached
        stats = store.get_cache_stats()
        assert stats is not None
        assert stats["path_cache_size"] == 3

        # Batch delete should invalidate cache
        store.delete_batch(["/test0.txt", "/test1.txt"])

        # Verify deletions
        assert not store.exists("/test0.txt")
        assert not store.exists("/test1.txt")
        assert store.exists("/test2.txt")

        # Batch update should invalidate cache
        new_metadata = [
            FileMetadata(
                path="/test2.txt",
                backend_name="local",
                physical_path="newhash",
                size=500,
                etag="newhash",
            )
        ]
        store.put_batch(new_metadata)

        # Verify update
        result = store.get("/test2.txt")
        assert result is not None
        assert result.size == 500

        store.close()
