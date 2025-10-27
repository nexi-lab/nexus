"""Unit tests for metadata caching."""

import gc
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from nexus import LocalBackend, NexusFS
from nexus.core.metadata import FileMetadata
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


def cleanup_windows_db():
    """Force cleanup of database connections on Windows."""
    gc.collect()  # Force garbage collection to release connections
    # Note: Windows file handle delays removed - using proper cleanup instead


class TestMetadataCache:
    """Test suite for MetadataCache functionality."""

    def test_cache_enabled_by_default(self, tmp_path: Path):
        """Test that caching is enabled by default."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Verify cache is enabled
        stats = store.get_cache_stats()
        assert stats is not None
        assert stats["path_cache_maxsize"] == 512
        assert stats["list_cache_maxsize"] == 128

        store.close()

    def test_cache_disabled(self, tmp_path: Path):
        """Test that caching can be disabled."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=False)

        # Verify cache is disabled
        stats = store.get_cache_stats()
        assert stats is None

        store.close()

    def test_path_metadata_caching(self, tmp_path: Path):
        """Test that get() results are cached."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store metadata
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash123",
            size=100,
            etag="hash123",
        )
        store.put(metadata)

        # First get - should hit database
        result1 = store.get("/test.txt")
        assert result1 is not None
        assert result1.path == "/test.txt"

        # Check cache stats
        stats = store.get_cache_stats()
        assert stats["path_cache_size"] == 1

        # Second get - should hit cache
        result2 = store.get("/test.txt")
        assert result2 is not None
        assert result2.path == "/test.txt"

        # Cache size should remain 1
        stats = store.get_cache_stats()
        assert stats["path_cache_size"] == 1

        store.close()

    def test_cache_invalidation_on_put(self, tmp_path: Path):
        """Test that cache is invalidated when file is updated."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store and cache metadata
        metadata1 = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash123",
            size=100,
            etag="hash123",
        )
        store.put(metadata1)
        store.get("/test.txt")  # Cache it

        # Update metadata
        metadata2 = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash456",
            size=200,
            etag="hash456",
        )
        store.put(metadata2)

        # Get should return updated metadata
        result = store.get("/test.txt")
        assert result is not None
        assert result.size == 200
        assert result.etag == "hash456"

        store.close()

    def test_cache_invalidation_on_delete(self, tmp_path: Path):
        """Test that cache is invalidated when file is deleted."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store and cache metadata
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash123",
            size=100,
            etag="hash123",
        )
        store.put(metadata)
        store.get("/test.txt")  # Cache it

        # Delete file
        store.delete("/test.txt")

        # Get should return None
        result = store.get("/test.txt")
        assert result is None

        store.close()

    def test_list_caching(self, tmp_path: Path):
        """Test that list() results are cached."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

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

        # First list - should hit database
        result1 = store.list("/test")
        assert len(result1) == 5

        # Check cache stats
        stats = store.get_cache_stats()
        assert stats["list_cache_size"] == 1

        # Second list - should hit cache
        result2 = store.list("/test")
        assert len(result2) == 5

        # Cache size should remain 1
        stats = store.get_cache_stats()
        assert stats["list_cache_size"] == 1

        store.close()

    def test_list_cache_invalidation(self, tmp_path: Path):
        """Test that list cache is invalidated when files are modified."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store files and cache list
        metadata1 = FileMetadata(
            path="/test1.txt",
            backend_name="local",
            physical_path="hash1",
            size=100,
            etag="hash1",
        )
        store.put(metadata1)
        store.list("")  # Cache empty prefix list

        # Add new file
        metadata2 = FileMetadata(
            path="/test2.txt",
            backend_name="local",
            physical_path="hash2",
            size=100,
            etag="hash2",
        )
        store.put(metadata2)

        # List should return updated results
        result = store.list("")
        assert len(result) == 2

        store.close()

    def test_exists_caching(self, tmp_path: Path):
        """Test that exists() results are cached."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store metadata
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash123",
            size=100,
            etag="hash123",
        )
        store.put(metadata)

        # First exists - should hit database
        result1 = store.exists("/test.txt")
        assert result1 is True

        # Check cache stats
        stats = store.get_cache_stats()
        assert stats["exists_cache_size"] == 1

        # Second exists - should hit cache
        result2 = store.exists("/test.txt")
        assert result2 is True

        # Cache size should remain 1
        stats = store.get_cache_stats()
        assert stats["exists_cache_size"] == 1

        store.close()

    def test_kv_metadata_caching(self, tmp_path: Path):
        """Test that get_file_metadata() results are cached."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store file
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash123",
            size=100,
            etag="hash123",
        )
        store.put(metadata)

        # Set metadata
        store.set_file_metadata("/test.txt", "key1", {"value": "test"})

        # First get - should hit database
        result1 = store.get_file_metadata("/test.txt", "key1")
        assert result1 == {"value": "test"}

        # Check cache stats
        stats = store.get_cache_stats()
        assert stats["kv_cache_size"] == 1

        # Second get - should hit cache
        result2 = store.get_file_metadata("/test.txt", "key1")
        assert result2 == {"value": "test"}

        # Cache size should remain 1
        stats = store.get_cache_stats()
        assert stats["kv_cache_size"] == 1

        store.close()

    def test_kv_cache_invalidation(self, tmp_path: Path):
        """Test that KV cache is invalidated when metadata is updated."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store file
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="hash123",
            size=100,
            etag="hash123",
        )
        store.put(metadata)

        # Set and cache metadata
        store.set_file_metadata("/test.txt", "key1", {"value": "test1"})
        store.get_file_metadata("/test.txt", "key1")

        # Update metadata
        store.set_file_metadata("/test.txt", "key1", {"value": "test2"})

        # Get should return updated value
        result = store.get_file_metadata("/test.txt", "key1")
        assert result == {"value": "test2"}

        store.close()

    @pytest.mark.slow
    def test_cache_ttl(self, tmp_path: Path):
        """Test that cache entries expire after TTL."""
        db_path = tmp_path / "test.db"
        # Set short TTL for testing
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True, cache_ttl_seconds=1)

        # Store and cache metadata
        with freeze_time("2025-01-01 12:00:00") as frozen_time:
            metadata = FileMetadata(
                path="/test.txt",
                backend_name="local",
                physical_path="hash123",
                size=100,
                etag="hash123",
            )
            store.put(metadata)
            result1 = store.get("/test.txt")
            assert result1 is not None

            # Advance time to expire the cache (TTL is 1 second)
            frozen_time.tick(delta=timedelta(seconds=1.5))

            # Entry should be expired (still returns correct data from DB)
            result2 = store.get("/test.txt")
            assert result2 is not None
            assert result2.path == "/test.txt"

        store.close()

    def test_cache_clear(self, tmp_path: Path):
        """Test that cache can be cleared."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Store and cache some data
        for i in range(3):
            metadata = FileMetadata(
                path=f"/test{i}.txt",
                backend_name="local",
                physical_path=f"hash{i}",
                size=100,
                etag=f"hash{i}",
            )
            store.put(metadata)
            store.get(f"/test{i}.txt")

        # Verify cache has entries
        stats = store.get_cache_stats()
        assert stats["path_cache_size"] > 0

        # Clear cache
        store.clear_cache()

        # Verify cache is empty
        stats = store.get_cache_stats()
        assert stats["path_cache_size"] == 0

        store.close()

    def test_embedded_with_cache(self):
        """Test that Embedded filesystem uses caching."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create filesystem with caching enabled
            fs = NexusFS(
                auto_parse=False,
                backend=LocalBackend(tmp_dir),
                db_path=Path(tmp_dir) / "metadata.db",
                enable_metadata_cache=True,
                cache_path_size=256,
                cache_ttl_seconds=300,
                enforce_permissions=False,  # Disable permissions for test
            )

            # Write some files
            fs.write("/test1.txt", b"Hello")
            fs.write("/test2.txt", b"World")

            # Read files (should be cached)
            content1 = fs.read("/test1.txt")
            assert content1 == b"Hello"

            # Check cache stats
            stats = fs.metadata.get_cache_stats()
            assert stats is not None
            assert stats["path_cache_size"] > 0

            fs.close()
            cleanup_windows_db()

    def test_embedded_without_cache(self):
        """Test that Embedded filesystem can disable caching."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create filesystem with caching disabled
            fs = NexusFS(
                auto_parse=False,
                backend=LocalBackend(tmp_dir),
                db_path=Path(tmp_dir) / "metadata.db",
                enable_metadata_cache=False,
                enforce_permissions=False,  # Disable permissions for test
            )

            # Write and read files
            fs.write("/test.txt", b"Hello")
            content = fs.read("/test.txt")
            assert content == b"Hello"

            # Check cache is disabled
            stats = fs.metadata.get_cache_stats()
            assert stats is None

            fs.close()
            cleanup_windows_db()

    def test_cache_with_negative_results(self, tmp_path: Path):
        """Test that negative results (not found) are also cached."""
        db_path = tmp_path / "test.db"
        store = SQLAlchemyMetadataStore(db_path, enable_cache=True)

        # Query non-existent file
        result1 = store.get("/nonexistent.txt")
        assert result1 is None

        # Check that None result is cached
        stats = store.get_cache_stats()
        assert stats["path_cache_size"] == 1

        # Second query should hit cache
        result2 = store.get("/nonexistent.txt")
        assert result2 is None

        store.close()
