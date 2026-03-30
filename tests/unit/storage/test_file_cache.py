"""Unit tests for FileContentCache (disk-based L2 cache)."""

from pathlib import Path

from nexus.storage.file_cache import FileContentCache


class TestFileContentCache:
    """Test suite for FileContentCache functionality."""

    def test_basic_write_and_read(self, tmp_path: Path):
        """Test basic write and read operations."""
        cache = FileContentCache(tmp_path)

        content = b"Hello World"
        cache.write("zone1", "/mnt/gcs/file.txt", content)

        result = cache.read("zone1", "/mnt/gcs/file.txt")
        assert result == content

    def test_write_with_text_content(self, tmp_path: Path):
        """Test writing binary and text content together."""
        cache = FileContentCache(tmp_path)

        content = b"Hello World"
        text_content = "Hello World"
        cache.write("zone1", "/mnt/gcs/file.txt", content, text_content=text_content)

        # Read binary
        result = cache.read("zone1", "/mnt/gcs/file.txt")
        assert result == content

        # Read text
        text_result = cache.read_text("zone1", "/mnt/gcs/file.txt")
        assert text_result == text_content

    def test_read_nonexistent(self, tmp_path: Path):
        """Test reading a non-existent file returns None."""
        cache = FileContentCache(tmp_path)

        result = cache.read("zone1", "/mnt/gcs/nonexistent.txt")
        assert result is None

        text_result = cache.read_text("zone1", "/mnt/gcs/nonexistent.txt")
        assert text_result is None

    def test_exists(self, tmp_path: Path):
        """Test exists check."""
        cache = FileContentCache(tmp_path)

        # Before write
        assert not cache.exists("zone1", "/mnt/gcs/file.txt")

        # After write
        cache.write("zone1", "/mnt/gcs/file.txt", b"content")
        assert cache.exists("zone1", "/mnt/gcs/file.txt")

    def test_delete(self, tmp_path: Path):
        """Test delete operation."""
        cache = FileContentCache(tmp_path)

        # Write content
        cache.write("zone1", "/mnt/gcs/file.txt", b"content", text_content="content")
        assert cache.exists("zone1", "/mnt/gcs/file.txt")

        # Delete
        deleted = cache.delete("zone1", "/mnt/gcs/file.txt")
        assert deleted

        # Verify deleted
        assert not cache.exists("zone1", "/mnt/gcs/file.txt")
        assert cache.read("zone1", "/mnt/gcs/file.txt") is None
        assert cache.read_text("zone1", "/mnt/gcs/file.txt") is None

    def test_delete_nonexistent(self, tmp_path: Path):
        """Test deleting non-existent file returns False."""
        cache = FileContentCache(tmp_path)

        deleted = cache.delete("zone1", "/mnt/gcs/nonexistent.txt")
        assert not deleted

    def test_zone_isolation(self, tmp_path: Path):
        """Test that zones are isolated."""
        cache = FileContentCache(tmp_path)

        # Write to two zones
        cache.write("zone1", "/file.txt", b"zone1 content")
        cache.write("zone2", "/file.txt", b"zone2 content")

        # Read should return correct content per zone
        assert cache.read("zone1", "/file.txt") == b"zone1 content"
        assert cache.read("zone2", "/file.txt") == b"zone2 content"

    def test_read_bulk(self, tmp_path: Path):
        """Test bulk read operation."""
        cache = FileContentCache(tmp_path)

        # Write multiple files
        for i in range(5):
            cache.write("zone1", f"/file{i}.txt", f"content{i}".encode())

        # Bulk read
        paths = [f"/file{i}.txt" for i in range(5)]
        results = cache.read_bulk("zone1", paths)

        assert len(results) == 5
        for i in range(5):
            assert results[f"/file{i}.txt"] == f"content{i}".encode()

    def test_read_bulk_with_missing(self, tmp_path: Path):
        """Test bulk read with some missing files."""
        cache = FileContentCache(tmp_path)

        # Write only some files
        cache.write("zone1", "/file1.txt", b"content1")
        cache.write("zone1", "/file3.txt", b"content3")

        # Bulk read including missing files
        paths = ["/file1.txt", "/file2.txt", "/file3.txt", "/file4.txt"]
        results = cache.read_bulk("zone1", paths)

        # Should only contain existing files
        assert len(results) == 2
        assert results["/file1.txt"] == b"content1"
        assert results["/file3.txt"] == b"content3"

    def test_read_text_bulk(self, tmp_path: Path):
        """Test bulk text read operation."""
        cache = FileContentCache(tmp_path)

        # Write multiple files with text
        for i in range(3):
            cache.write("zone1", f"/file{i}.txt", b"binary", text_content=f"text{i}")

        # Bulk read text
        paths = [f"/file{i}.txt" for i in range(3)]
        results = cache.read_text_bulk("zone1", paths)

        assert len(results) == 3
        for i in range(3):
            assert results[f"/file{i}.txt"] == f"text{i}"

    def test_delete_zone(self, tmp_path: Path):
        """Test deleting all files for a zone."""
        cache = FileContentCache(tmp_path)

        # Write files for multiple zones
        for i in range(5):
            cache.write("zone1", f"/file{i}.txt", b"content")
        cache.write("zone2", "/file.txt", b"content")

        # Delete zone1
        deleted_count = cache.delete_zone("zone1")
        assert deleted_count == 5

        # Verify zone1 files are gone
        for i in range(5):
            assert not cache.exists("zone1", f"/file{i}.txt")

        # Zone2 should be unaffected
        assert cache.exists("zone2", "/file.txt")

    def test_cache_stats(self, tmp_path: Path):
        """Test cache statistics."""
        cache = FileContentCache(tmp_path)

        # Initially empty
        stats = cache.get_cache_stats()
        assert stats["total_files"] == 0
        assert stats["total_size_bytes"] == 0

        # Write some files
        cache.write("zone1", "/file1.txt", b"12345")  # 5 bytes
        cache.write("zone1", "/file2.txt", b"1234567890")  # 10 bytes
        cache.write("zone2", "/file.txt", b"abc")  # 3 bytes

        # Check stats
        stats = cache.get_cache_stats()
        assert stats["total_files"] == 3
        assert stats["total_size_bytes"] == 18
        assert stats["zones"]["zone1"]["files"] == 2
        assert stats["zones"]["zone1"]["size_bytes"] == 15
        assert stats["zones"]["zone2"]["files"] == 1
        assert stats["zones"]["zone2"]["size_bytes"] == 3

    def test_zoekt_index_path(self, tmp_path: Path):
        """Test Zoekt index path returns cache directory."""
        cache = FileContentCache(tmp_path)

        zoekt_path = cache.get_zoekt_index_path()
        assert zoekt_path == tmp_path / ".cache"

    def test_hash_based_sharding(self, tmp_path: Path):
        """Test that files are sharded by hash."""
        cache = FileContentCache(tmp_path)

        # Write a file
        cache.write("zone1", "/mnt/gcs/test.txt", b"content")

        # Check that it's stored with hash-based path
        cache_dir = tmp_path / ".cache" / "zone1"

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
        cache.write("zone1", "/file.txt", b"content1")
        cache.write("zone1", "/file.txt", b"content2")

        # Should only have one file (overwritten)
        stats = cache.get_cache_stats()
        assert stats["total_files"] == 1

        # Content should be the newer one
        assert cache.read("zone1", "/file.txt") == b"content2"


class TestFileContentCacheStaleness:
    """Test suite for lease-aware staleness tracking (Issue #3400)."""

    def test_basic_staleness_read_returns_none(self, tmp_path: Path):
        """Write file, mark stale, read returns None."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"hello")

        cache.mark_lease_revoked("zone1", "/file.txt")

        assert cache.read("zone1", "/file.txt") is None

    def test_staleness_cleared_on_write(self, tmp_path: Path):
        """Mark stale, write new content, read succeeds."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"original")

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") is None

        cache.write("zone1", "/file.txt", b"refreshed")
        assert cache.read("zone1", "/file.txt") == b"refreshed"

    def test_lease_acquired_clears_staleness(self, tmp_path: Path):
        """Mark stale, then mark lease acquired, read succeeds."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") is None

        cache.mark_lease_acquired("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") == b"content"

    def test_is_stale_basic(self, tmp_path: Path):
        """is_stale() returns correct values across lifecycle."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        assert not cache.is_stale("zone1", "/file.txt")

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert cache.is_stale("zone1", "/file.txt")

        cache.mark_lease_acquired("zone1", "/file.txt")
        assert not cache.is_stale("zone1", "/file.txt")

    def test_has_active_lease_basic(self, tmp_path: Path):
        """has_active_lease() returns correct values across lifecycle."""
        cache = FileContentCache(tmp_path)

        assert not cache.has_active_lease("zone1", "/file.txt")

        cache.mark_lease_acquired("zone1", "/file.txt")
        assert cache.has_active_lease("zone1", "/file.txt")

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert not cache.has_active_lease("zone1", "/file.txt")

    def test_read_text_returns_none_when_stale(self, tmp_path: Path):
        """read_text() returns None for stale paths."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"binary", text_content="text content")

        assert cache.read_text("zone1", "/file.txt") == "text content"

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert cache.read_text("zone1", "/file.txt") is None

    def test_read_meta_returns_none_when_stale(self, tmp_path: Path):
        """read_meta() returns None for stale paths."""
        cache = FileContentCache(tmp_path)
        meta = {"content_hash": "abc123", "content_type": "full"}
        cache.write("zone1", "/file.txt", b"data", meta=meta)

        assert cache.read_meta("zone1", "/file.txt") is not None

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert cache.read_meta("zone1", "/file.txt") is None

    def test_staleness_is_per_path(self, tmp_path: Path):
        """Stale path A does not affect path B in the same zone."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/a.txt", b"content a")
        cache.write("zone1", "/b.txt", b"content b")

        cache.mark_lease_revoked("zone1", "/a.txt")

        assert cache.read("zone1", "/a.txt") is None
        assert cache.read("zone1", "/b.txt") == b"content b"

    def test_staleness_is_per_zone(self, tmp_path: Path):
        """Stale zone1/path does not affect zone2/path."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"zone1 data")
        cache.write("zone2", "/file.txt", b"zone2 data")

        cache.mark_lease_revoked("zone1", "/file.txt")

        assert cache.read("zone1", "/file.txt") is None
        assert cache.read("zone2", "/file.txt") == b"zone2 data"

    def test_mark_revoked_without_prior_acquire(self, tmp_path: Path):
        """Revoking a lease that was never acquired should work fine."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        # No prior mark_lease_acquired — should not raise
        cache.mark_lease_revoked("zone1", "/file.txt")

        assert cache.is_stale("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") is None

    def test_multiple_revocations_idempotent(self, tmp_path: Path):
        """Multiple mark_lease_revoked calls are idempotent."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        cache.mark_lease_revoked("zone1", "/file.txt")
        cache.mark_lease_revoked("zone1", "/file.txt")
        cache.mark_lease_revoked("zone1", "/file.txt")

        assert cache.is_stale("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") is None

    def test_full_lease_lifecycle(self, tmp_path: Path):
        """Lease acquired, then revoked, then acquired again."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        # Acquire
        cache.mark_lease_acquired("zone1", "/file.txt")
        assert cache.has_active_lease("zone1", "/file.txt")
        assert not cache.is_stale("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") == b"content"

        # Revoke
        cache.mark_lease_revoked("zone1", "/file.txt")
        assert not cache.has_active_lease("zone1", "/file.txt")
        assert cache.is_stale("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") is None

        # Re-acquire
        cache.mark_lease_acquired("zone1", "/file.txt")
        assert cache.has_active_lease("zone1", "/file.txt")
        assert not cache.is_stale("zone1", "/file.txt")
        assert cache.read("zone1", "/file.txt") == b"content"

    def test_bulk_read_with_stale_and_fresh(self, tmp_path: Path):
        """Bulk read returns only fresh paths, skipping stale ones."""
        cache = FileContentCache(tmp_path)
        for i in range(5):
            cache.write("zone1", f"/file{i}.txt", f"content{i}".encode())

        # Mark files 1 and 3 as stale
        cache.mark_lease_revoked("zone1", "/file1.txt")
        cache.mark_lease_revoked("zone1", "/file3.txt")

        paths = [f"/file{i}.txt" for i in range(5)]
        results = cache.read_bulk("zone1", paths)

        assert "/file0.txt" in results
        assert "/file1.txt" not in results
        assert "/file2.txt" in results
        assert "/file3.txt" not in results
        assert "/file4.txt" in results
        assert len(results) == 3

    def test_exists_returns_true_for_stale_paths(self, tmp_path: Path):
        """exists() still returns True for stale paths (file is on disk)."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        cache.mark_lease_revoked("zone1", "/file.txt")

        # File is on disk, exists() should still be True
        assert cache.exists("zone1", "/file.txt")
        # But read should return None due to staleness
        assert cache.read("zone1", "/file.txt") is None

    def test_delete_clears_staleness(self, tmp_path: Path):
        """delete() clears staleness so the path is no longer marked stale."""
        cache = FileContentCache(tmp_path)
        cache.write("zone1", "/file.txt", b"content")

        cache.mark_lease_revoked("zone1", "/file.txt")
        assert cache.is_stale("zone1", "/file.txt")

        cache.delete("zone1", "/file.txt")
        assert not cache.is_stale("zone1", "/file.txt")

    def test_thread_safety_concurrent_revoke_and_read(self, tmp_path: Path):
        """Concurrent mark_lease_revoked and read operations do not crash."""
        import threading

        cache = FileContentCache(tmp_path)
        errors: list[Exception] = []

        # Write files
        for i in range(20):
            cache.write("zone1", f"/file{i}.txt", f"content{i}".encode())

        def revoke_loop():
            try:
                for _ in range(100):
                    for i in range(20):
                        cache.mark_lease_revoked("zone1", f"/file{i}.txt")
            except Exception as e:
                errors.append(e)

        def read_loop():
            try:
                for _ in range(100):
                    for i in range(20):
                        cache.read("zone1", f"/file{i}.txt")
            except Exception as e:
                errors.append(e)

        def acquire_loop():
            try:
                for _ in range(100):
                    for i in range(20):
                        cache.mark_lease_acquired("zone1", f"/file{i}.txt")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=revoke_loop),
            threading.Thread(target=read_loop),
            threading.Thread(target=acquire_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
