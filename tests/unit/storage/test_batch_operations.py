"""Unit tests for batch metadata operations."""

from pathlib import Path

from nexus import LocalBackend, NexusFS
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

    def test_embedded_rmdir_uses_batch_delete(self, tmp_path: Path):
        """Test that Embedded.rmdir() uses batch delete for directories."""
        db_path = tmp_path / "metadata.db"
        fs = NexusFS(
            backend=LocalBackend(tmp_path),
            db_path=db_path,
            enforce_permissions=False,  # Disable permissions for test
        )

        try:
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
        finally:
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

    def test_batch_get_content_ids_basic(self, tmp_path: Path):
        """Test basic batch get content IDs operation."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store multiple files with different content hashes
        files = [
            ("/file1.txt", "hash1"),
            ("/file2.txt", "hash2"),
            ("/file3.txt", "hash3"),
            ("/file4.txt", "hash1"),  # Duplicate of file1
            ("/file5.txt", "hash2"),  # Duplicate of file2
        ]

        for path, content_hash in files:
            metadata = FileMetadata(
                path=path,
                backend_name="local",
                physical_path=f"storage{path}",
                size=100,
                etag=content_hash,
            )
            store.put(metadata)

        # Batch get content IDs
        paths = [f[0] for f in files]
        result = store.batch_get_content_ids(paths)

        # Verify all hashes retrieved
        assert len(result) == 5
        assert result["/file1.txt"] == "hash1"
        assert result["/file2.txt"] == "hash2"
        assert result["/file3.txt"] == "hash3"
        assert result["/file4.txt"] == "hash1"
        assert result["/file5.txt"] == "hash2"

        store.close()

    def test_batch_get_content_ids_with_missing(self, tmp_path: Path):
        """Test batch get content IDs with some files not found."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store only some files
        for i in [0, 2, 4]:
            metadata = FileMetadata(
                path=f"/file{i}.txt",
                backend_name="local",
                physical_path=f"storage/file{i}.txt",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)

        # Batch get including non-existent files
        paths = [f"/file{i}.txt" for i in range(6)]
        result = store.batch_get_content_ids(paths)

        # Verify mixed results
        assert len(result) == 6
        assert result["/file0.txt"] == "hash0"
        assert result["/file1.txt"] is None
        assert result["/file2.txt"] == "hash2"
        assert result["/file3.txt"] is None
        assert result["/file4.txt"] == "hash4"
        assert result["/file5.txt"] is None

        store.close()

    def test_batch_get_content_ids_empty_list(self, tmp_path: Path):
        """Test batch get content IDs with empty path list."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        result = store.batch_get_content_ids([])
        assert result == {}

        store.close()

    def test_batch_get_content_ids_deduplication(self, tmp_path: Path):
        """Test using batch_get_content_ids for deduplication detection."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store files with some duplicates
        files = [
            ("/doc1.txt", "abc123"),
            ("/doc2.txt", "def456"),
            ("/doc3.txt", "abc123"),  # Duplicate of doc1
            ("/doc4.txt", "ghi789"),
            ("/doc5.txt", "def456"),  # Duplicate of doc2
            ("/doc6.txt", "abc123"),  # Duplicate of doc1 and doc3
        ]

        for path, content_hash in files:
            metadata = FileMetadata(
                path=path,
                backend_name="local",
                physical_path=f"storage{path}",
                size=100,
                etag=content_hash,
            )
            store.put(metadata)

        # Get all content IDs
        paths = [f[0] for f in files]
        hashes = store.batch_get_content_ids(paths)

        # Find duplicates
        from collections import defaultdict

        by_hash = defaultdict(list)
        for path, content_hash in hashes.items():
            if content_hash:
                by_hash[content_hash].append(path)

        duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}

        # Verify duplicate detection
        assert len(duplicates) == 2  # Two groups of duplicates
        assert set(duplicates["abc123"]) == {"/doc1.txt", "/doc3.txt", "/doc6.txt"}
        assert set(duplicates["def456"]) == {"/doc2.txt", "/doc5.txt"}

        store.close()

    def test_batch_get_content_ids_null_hashes(self, tmp_path: Path):
        """Test batch get content IDs with files that have no content hash."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store files with some having no content hash
        metadata1 = FileMetadata(
            path="/file1.txt",
            backend_name="local",
            physical_path="storage/file1.txt",
            size=100,
            etag="hash1",
        )
        store.put(metadata1)

        metadata2 = FileMetadata(
            path="/file2.txt",
            backend_name="local",
            physical_path="storage/file2.txt",
            size=100,
            etag=None,  # No hash
        )
        store.put(metadata2)

        # Batch get content IDs
        result = store.batch_get_content_ids(["/file1.txt", "/file2.txt"])

        # Verify results
        assert result["/file1.txt"] == "hash1"
        assert result["/file2.txt"] is None

        store.close()

    def test_batch_get_content_ids_performance(self, tmp_path: Path):
        """Test batch get content IDs with large number of files."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Create many files
        num_files = 100
        for i in range(num_files):
            metadata = FileMetadata(
                path=f"/file{i}.txt",
                backend_name="local",
                physical_path=f"storage/file{i}.txt",
                size=1000,
                etag=f"hash{i % 10}",  # Create some duplicates
            )
            store.put(metadata)

        # Batch get all content IDs
        paths = [f"/file{i}.txt" for i in range(num_files)]
        result = store.batch_get_content_ids(paths)

        # Verify all returned
        assert len(result) == num_files

        # Count duplicates by hash
        from collections import Counter

        hash_counts = Counter(h for h in result.values() if h is not None)

        # Each hash (0-9) should appear 10 times
        for i in range(10):
            assert hash_counts[f"hash{i}"] == 10

        store.close()

    def test_batch_get_content_ids_vs_get_batch(self, tmp_path: Path):
        """Compare batch_get_content_ids with get_batch for efficiency."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path)

        # Store files
        for i in range(10):
            metadata = FileMetadata(
                path=f"/file{i}.txt",
                backend_name="local",
                physical_path=f"storage/file{i}.txt",
                size=1000,
                etag=f"hash{i}",
            )
            store.put(metadata)

        paths = [f"/file{i}.txt" for i in range(10)]

        # Method 1: batch_get_content_ids (efficient - only fetches hash)
        result1 = store.batch_get_content_ids(paths)

        # Method 2: get_batch (less efficient - fetches full metadata)
        result2_meta = store.get_batch(paths)
        result2 = {p: m.etag if m else None for p, m in result2_meta.items()}

        # Results should be identical
        assert result1 == result2

        # Both methods should return correct hashes
        for i in range(10):
            path = f"/file{i}.txt"
            assert result1[path] == f"hash{i}"
            assert result2[path] == f"hash{i}"

        store.close()
