"""Unit tests for FileContentCache (disk-based L2 cache)."""

from pathlib import Path

from nexus.storage.file_cache import FileContentCache, get_file_cache


class TestFileContentCache:
    """Test suite for FileContentCache functionality."""

    def test_basic_write_and_read(self, tmp_path: Path):
        """Test basic write and read operations."""
        cache = FileContentCache(tmp_path)

        content = b"Hello World"
        cache.write("tenant1", "/mnt/gcs/file.txt", content)

        result = cache.read("tenant1", "/mnt/gcs/file.txt")
        assert result == content

    def test_write_with_text_content(self, tmp_path: Path):
        """Test writing binary and text content together."""
        cache = FileContentCache(tmp_path)

        content = b"Hello World"
        text_content = "Hello World"
        cache.write("tenant1", "/mnt/gcs/file.txt", content, text_content=text_content)

        # Read binary
        result = cache.read("tenant1", "/mnt/gcs/file.txt")
        assert result == content

        # Read text
        text_result = cache.read_text("tenant1", "/mnt/gcs/file.txt")
        assert text_result == text_content

    def test_read_nonexistent(self, tmp_path: Path):
        """Test reading a non-existent file returns None."""
        cache = FileContentCache(tmp_path)

        result = cache.read("tenant1", "/mnt/gcs/nonexistent.txt")
        assert result is None

        text_result = cache.read_text("tenant1", "/mnt/gcs/nonexistent.txt")
        assert text_result is None

    def test_exists(self, tmp_path: Path):
        """Test exists check."""
        cache = FileContentCache(tmp_path)

        # Before write
        assert not cache.exists("tenant1", "/mnt/gcs/file.txt")

        # After write
        cache.write("tenant1", "/mnt/gcs/file.txt", b"content")
        assert cache.exists("tenant1", "/mnt/gcs/file.txt")

    def test_delete(self, tmp_path: Path):
        """Test delete operation."""
        cache = FileContentCache(tmp_path)

        # Write content
        cache.write("tenant1", "/mnt/gcs/file.txt", b"content", text_content="content")
        assert cache.exists("tenant1", "/mnt/gcs/file.txt")

        # Delete
        deleted = cache.delete("tenant1", "/mnt/gcs/file.txt")
        assert deleted

        # Verify deleted
        assert not cache.exists("tenant1", "/mnt/gcs/file.txt")
        assert cache.read("tenant1", "/mnt/gcs/file.txt") is None
        assert cache.read_text("tenant1", "/mnt/gcs/file.txt") is None

    def test_delete_nonexistent(self, tmp_path: Path):
        """Test deleting non-existent file returns False."""
        cache = FileContentCache(tmp_path)

        deleted = cache.delete("tenant1", "/mnt/gcs/nonexistent.txt")
        assert not deleted

    def test_tenant_isolation(self, tmp_path: Path):
        """Test that tenants are isolated."""
        cache = FileContentCache(tmp_path)

        # Write to two tenants
        cache.write("tenant1", "/file.txt", b"tenant1 content")
        cache.write("tenant2", "/file.txt", b"tenant2 content")

        # Read should return correct content per tenant
        assert cache.read("tenant1", "/file.txt") == b"tenant1 content"
        assert cache.read("tenant2", "/file.txt") == b"tenant2 content"

    def test_read_bulk(self, tmp_path: Path):
        """Test bulk read operation."""
        cache = FileContentCache(tmp_path)

        # Write multiple files
        for i in range(5):
            cache.write("tenant1", f"/file{i}.txt", f"content{i}".encode())

        # Bulk read
        paths = [f"/file{i}.txt" for i in range(5)]
        results = cache.read_bulk("tenant1", paths)

        assert len(results) == 5
        for i in range(5):
            assert results[f"/file{i}.txt"] == f"content{i}".encode()

    def test_read_bulk_with_missing(self, tmp_path: Path):
        """Test bulk read with some missing files."""
        cache = FileContentCache(tmp_path)

        # Write only some files
        cache.write("tenant1", "/file1.txt", b"content1")
        cache.write("tenant1", "/file3.txt", b"content3")

        # Bulk read including missing files
        paths = ["/file1.txt", "/file2.txt", "/file3.txt", "/file4.txt"]
        results = cache.read_bulk("tenant1", paths)

        # Should only contain existing files
        assert len(results) == 2
        assert results["/file1.txt"] == b"content1"
        assert results["/file3.txt"] == b"content3"

    def test_read_text_bulk(self, tmp_path: Path):
        """Test bulk text read operation."""
        cache = FileContentCache(tmp_path)

        # Write multiple files with text
        for i in range(3):
            cache.write("tenant1", f"/file{i}.txt", b"binary", text_content=f"text{i}")

        # Bulk read text
        paths = [f"/file{i}.txt" for i in range(3)]
        results = cache.read_text_bulk("tenant1", paths)

        assert len(results) == 3
        for i in range(3):
            assert results[f"/file{i}.txt"] == f"text{i}"

    def test_delete_tenant(self, tmp_path: Path):
        """Test deleting all files for a tenant."""
        cache = FileContentCache(tmp_path)

        # Write files for multiple tenants
        for i in range(5):
            cache.write("tenant1", f"/file{i}.txt", b"content")
        cache.write("tenant2", "/file.txt", b"content")

        # Delete tenant1
        deleted_count = cache.delete_tenant("tenant1")
        assert deleted_count == 5

        # Verify tenant1 files are gone
        for i in range(5):
            assert not cache.exists("tenant1", f"/file{i}.txt")

        # Tenant2 should be unaffected
        assert cache.exists("tenant2", "/file.txt")

    def test_cache_stats(self, tmp_path: Path):
        """Test cache statistics."""
        cache = FileContentCache(tmp_path)

        # Initially empty
        stats = cache.get_cache_stats()
        assert stats["total_files"] == 0
        assert stats["total_size_bytes"] == 0

        # Write some files
        cache.write("tenant1", "/file1.txt", b"12345")  # 5 bytes
        cache.write("tenant1", "/file2.txt", b"1234567890")  # 10 bytes
        cache.write("tenant2", "/file.txt", b"abc")  # 3 bytes

        # Check stats
        stats = cache.get_cache_stats()
        assert stats["total_files"] == 3
        assert stats["total_size_bytes"] == 18
        assert stats["tenants"]["tenant1"]["files"] == 2
        assert stats["tenants"]["tenant1"]["size_bytes"] == 15
        assert stats["tenants"]["tenant2"]["files"] == 1
        assert stats["tenants"]["tenant2"]["size_bytes"] == 3

    def test_zoekt_index_path(self, tmp_path: Path):
        """Test Zoekt index path returns cache directory."""
        cache = FileContentCache(tmp_path)

        zoekt_path = cache.get_zoekt_index_path()
        assert zoekt_path == tmp_path / ".cache"

    def test_hash_based_sharding(self, tmp_path: Path):
        """Test that files are sharded by hash."""
        cache = FileContentCache(tmp_path)

        # Write a file
        cache.write("tenant1", "/mnt/gcs/test.txt", b"content")

        # Check that it's stored with hash-based path
        cache_dir = tmp_path / ".cache" / "tenant1"

        # Find the file (should be in hash subdirectory)
        files = list(cache_dir.rglob("*.bin"))
        assert len(files) == 1

        # Path should be: hash[:2]/hash[2:4]/hash.bin
        relative = files[0].relative_to(cache_dir)
        parts = relative.parts
        assert len(parts) == 3  # [hash[:2], hash[2:4], hash.bin]

    def test_path_hash_consistency(self, tmp_path: Path):
        """Test that the same path always maps to the same cache location."""
        cache = FileContentCache(tmp_path)

        # Write same path twice
        cache.write("tenant1", "/file.txt", b"content1")
        cache.write("tenant1", "/file.txt", b"content2")

        # Should only have one file (overwritten)
        stats = cache.get_cache_stats()
        assert stats["total_files"] == 1

        # Content should be the newer one
        assert cache.read("tenant1", "/file.txt") == b"content2"


class TestGetFileCache:
    """Test global file cache accessor."""

    def test_get_file_cache_creates_singleton(self, tmp_path: Path, monkeypatch):
        """Test that get_file_cache returns the same instance."""
        import nexus.storage.file_cache as fc

        # Reset the global instance
        fc._file_cache = None

        # Set env var for cache dir
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))

        cache1 = get_file_cache()
        cache2 = get_file_cache()

        assert cache1 is cache2

        # Clean up
        fc._file_cache = None

    def test_get_file_cache_with_explicit_dir(self, tmp_path: Path):
        """Test get_file_cache with explicit directory."""
        import nexus.storage.file_cache as fc

        # Reset the global instance
        fc._file_cache = None

        cache = get_file_cache(tmp_path)
        assert cache.base_dir == tmp_path

        # Clean up
        fc._file_cache = None
