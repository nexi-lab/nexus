"""End-to-end tests for CDC Chunked Storage via FastAPI (Issue #1074)."""

import os

import pytest

from nexus.backends.engines.cdc import CDC_THRESHOLD_BYTES


class TestChunkedStorageE2E:
    """End-to-end tests for chunked storage through NexusFS."""

    @pytest.mark.asyncio
    async def test_small_file_not_chunked(self, nexus_fs) -> None:
        """Test that small files work normally (not chunked)."""
        content = b"This is a small test file content."

        # Write
        nexus_fs.write("/test_small.txt", content)

        # Read back
        read_content = nexus_fs.sys_read("/test_small.txt")
        assert read_content == content

        # Verify not chunked
        etag = nexus_fs.get_etag("/test_small.txt")
        assert not nexus_fs._kernel.cas_is_chunked("/", "root", etag), (
            "Small file should not be chunked"
        )

    @pytest.mark.asyncio
    async def test_large_file_chunked_write_read(self, nexus_fs) -> None:
        """Test that large files are chunked and can be read back correctly."""
        # Create content larger than CDC threshold (~17MB)
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        # Write
        nexus_fs.write("/test_large.bin", large_content)

        # Verify it was chunked
        etag = nexus_fs.get_etag("/test_large.bin")
        assert etag is not None

        assert nexus_fs._kernel.cas_is_chunked("/", "root", etag), "Large file should be chunked"

        # Read back
        read_content = nexus_fs.sys_read("/test_large.bin")
        assert read_content == large_content, "Content mismatch after chunked read"

    @pytest.mark.asyncio
    async def test_large_file_chunks_created(self, nexus_fs) -> None:
        """Test that a large file is chunked and all chunks are accessible."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        nexus_fs.write("/chunks_test.bin", large_content)

        etag = nexus_fs.get_etag("/chunks_test.bin")
        assert nexus_fs._kernel.cas_is_chunked("/", "root", etag), "Large file should be chunked"
        # Verify content round-trips correctly
        read_back = nexus_fs.sys_read("/chunks_test.bin")
        assert read_back == large_content

    @pytest.mark.asyncio
    async def test_large_file_delete_cleans_chunks(self, nexus_fs) -> None:
        """Test that deleting chunked files + GC removes CAS content.

        Issue #1320: sys_unlink is metadata-only — content cleanup is
        deferred to CAS GC.  After sys_unlink, cas_delete() simulates GC.
        """
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        nexus_fs.write("/delete_test.bin", large_content)
        content_id = nexus_fs.get_etag("/delete_test.bin")

        # Verify chunked and content exists
        assert nexus_fs._kernel.cas_is_chunked("/", "root", content_id)
        assert nexus_fs._kernel.cas_exists("/", "root", content_id)

        # Delete VFS metadata
        nexus_fs.sys_unlink("/delete_test.bin")
        assert nexus_fs.sys_stat("/delete_test.bin") is None

        # Simulate GC cleanup
        nexus_fs._kernel.cas_delete("/", "root", content_id)

        # Content hash should no longer exist
        assert not nexus_fs._kernel.cas_exists("/", "root", content_id)

    @pytest.mark.asyncio
    async def test_file_size_correct_for_chunked(self, nexus_fs) -> None:
        """Test that file metadata shows original size, not manifest size."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 2_000_000)
        original_size = len(large_content)

        nexus_fs.write("/size_test.bin", large_content)

        metadata = nexus_fs.sys_stat("/size_test.bin")
        assert metadata["size"] == original_size

    @pytest.mark.asyncio
    async def test_chunked_deduplication(self, nexus_fs) -> None:
        """Test that identical content shares chunks."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        # Write same content twice
        nexus_fs.write("/dedup_a.bin", large_content)
        nexus_fs.write("/dedup_b.bin", large_content)

        etag_a = nexus_fs.get_etag("/dedup_a.bin")
        etag_b = nexus_fs.get_etag("/dedup_b.bin")

        # Same content = same hash = same chunks
        assert etag_a == etag_b

        # Read both back
        assert nexus_fs.sys_read("/dedup_a.bin") == large_content
        assert nexus_fs.sys_read("/dedup_b.bin") == large_content

        # Delete one, other should still work (dedup)
        nexus_fs.sys_unlink("/dedup_a.bin")
        assert nexus_fs.sys_read("/dedup_b.bin") == large_content


class TestChunkedStorageBackwardCompatibility:
    """Test backward compatibility with existing single-blob storage."""

    @pytest.mark.asyncio
    async def test_mixed_small_and_large_files(self, nexus_fs) -> None:
        """Test that small and large files coexist correctly."""
        small_content = b"Small file content"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        # Write both types
        nexus_fs.write("/small.txt", small_content)
        nexus_fs.write("/large.bin", large_content)

        # Verify small not chunked, large chunked
        small_etag = nexus_fs.get_etag("/small.txt")
        large_etag = nexus_fs.get_etag("/large.bin")

        assert not nexus_fs._kernel.cas_is_chunked("/", "root", small_etag)
        assert nexus_fs._kernel.cas_is_chunked("/", "root", large_etag)

        # Read both
        assert nexus_fs.sys_read("/small.txt") == small_content
        assert nexus_fs.sys_read("/large.bin") == large_content

        # List directory
        files = nexus_fs.sys_readdir("/")
        assert "/small.txt" in files or "small.txt" in files
        assert "/large.bin" in files or "large.bin" in files

        # Delete both
        nexus_fs.sys_unlink("/small.txt")
        nexus_fs.sys_unlink("/large.bin")

        # Verify deleted
        assert nexus_fs.sys_stat("/small.txt") is None
        assert nexus_fs.sys_stat("/large.bin") is None

    @pytest.mark.asyncio
    async def test_overwrite_small_with_large(self, nexus_fs) -> None:
        """Test overwriting a small file with a large chunked file."""
        small_content = b"Initial small content"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)

        # Write small
        nexus_fs.write("/overwrite.bin", small_content)
        etag1 = nexus_fs.get_etag("/overwrite.bin")

        assert not nexus_fs._kernel.cas_is_chunked("/", "root", etag1)

        # Overwrite with large
        nexus_fs.write("/overwrite.bin", large_content)
        etag2 = nexus_fs.get_etag("/overwrite.bin")

        assert nexus_fs._kernel.cas_is_chunked("/", "root", etag2)
        assert nexus_fs.sys_read("/overwrite.bin") == large_content

    @pytest.mark.asyncio
    async def test_overwrite_large_with_small(self, nexus_fs) -> None:
        """Test overwriting a large chunked file with a small file."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)
        small_content = b"New small content"

        # Write large
        nexus_fs.write("/overwrite2.bin", large_content)
        etag1 = nexus_fs.get_etag("/overwrite2.bin")

        assert nexus_fs._kernel.cas_is_chunked("/", "root", etag1)

        # Overwrite with small
        nexus_fs.write("/overwrite2.bin", small_content)
        etag2 = nexus_fs.get_etag("/overwrite2.bin")

        assert not nexus_fs._kernel.cas_is_chunked("/", "root", etag2)
        assert nexus_fs.sys_read("/overwrite2.bin") == small_content


class TestChunkedStorageCDCBehavior:
    """Tests for CDC-specific behavior."""

    @pytest.mark.asyncio
    async def test_similar_files_share_chunks(self, nexus_fs) -> None:
        """Test that similar files are both chunked and round-trip correctly.

        CDC dedup (shared chunks) is a Rust-internal optimization —
        verified by Rust unit tests, not observable from Python without
        inspecting raw CAS blobs.
        """
        common_prefix = os.urandom(CDC_THRESHOLD_BYTES)
        suffix_a = os.urandom(1024 * 1024)
        suffix_b = os.urandom(1024 * 1024)

        content_a = common_prefix + suffix_a
        content_b = common_prefix + suffix_b

        nexus_fs.write("/similar_a.bin", content_a)
        nexus_fs.write("/similar_b.bin", content_b)

        etag_a = nexus_fs.get_etag("/similar_a.bin")
        etag_b = nexus_fs.get_etag("/similar_b.bin")

        # Both should be chunked
        assert nexus_fs._kernel.cas_is_chunked("/", "root", etag_a)
        assert nexus_fs._kernel.cas_is_chunked("/", "root", etag_b)

        # Both should round-trip correctly
        assert nexus_fs.sys_read("/similar_a.bin") == content_a
        assert nexus_fs.sys_read("/similar_b.bin") == content_b
