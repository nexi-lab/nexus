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
        """Test that individual chunks are created in CAS."""
        from nexus.backends.engines.cdc import ChunkedReference

        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        nexus_fs.write("/chunks_test.bin", large_content)

        etag = nexus_fs.get_etag("/chunks_test.bin")
        backend = nexus_fs.router.route("/").backend

        # Read manifest
        manifest_path = backend._transport._resolve(backend._blob_key(etag))
        manifest = ChunkedReference.from_json(manifest_path.read_bytes())

        # Verify each chunk exists
        for chunk_info in manifest.chunks:
            chunk_path = backend._transport._resolve(backend._blob_key(chunk_info.chunk_hash))
            assert chunk_path.exists(), f"Chunk {chunk_info.chunk_hash[:16]}... should exist"
            assert chunk_path.stat().st_size == chunk_info.length

    @pytest.mark.asyncio
    async def test_large_file_delete_cleans_chunks(self, nexus_fs) -> None:
        """Test that deleting chunked files cleans up chunks.

        Issue #1320: sys_unlink is now metadata-only — content cleanup is
        deferred to CAS GC via OBSERVE observer.  After sys_unlink removes
        VFS metadata, we call backend.delete_content() directly to simulate
        the GC cleanup and verify that chunks are properly removed.
        """
        from nexus.backends.engines.cdc import ChunkedReference

        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        # Write
        nexus_fs.write("/delete_test.bin", large_content)
        content_hash = nexus_fs.get_etag("/delete_test.bin")

        backend = nexus_fs.router.route("/").backend

        # Get chunk hashes before delete
        manifest = ChunkedReference.from_json(
            backend._transport._resolve(backend._blob_key(content_hash)).read_bytes()
        )
        chunk_hashes = [c.chunk_hash for c in manifest.chunks]

        # Verify chunks exist
        for ch in chunk_hashes:
            assert backend._transport._resolve(backend._blob_key(ch)).exists()

        # Delete VFS metadata
        nexus_fs.sys_unlink("/delete_test.bin")

        # Verify file is gone from VFS
        assert nexus_fs.sys_stat("/delete_test.bin") is None

        # Simulate GC cleanup: delete_content handles chunk ref-counting
        backend.delete_content(content_hash)

        # Verify chunks are deleted
        for ch in chunk_hashes:
            assert not backend._transport._resolve(backend._blob_key(ch)).exists(), (
                f"Chunk {ch[:16]}... should be deleted"
            )

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
        """Test that similar files (with common prefix) share some chunks."""
        from nexus.backends.engines.cdc import ChunkedReference

        # Create two files with identical prefix but different suffix
        common_prefix = os.urandom(CDC_THRESHOLD_BYTES)
        suffix_a = os.urandom(1024 * 1024)
        suffix_b = os.urandom(1024 * 1024)

        content_a = common_prefix + suffix_a
        content_b = common_prefix + suffix_b

        nexus_fs.write("/similar_a.bin", content_a)
        nexus_fs.write("/similar_b.bin", content_b)

        etag_a = nexus_fs.get_etag("/similar_a.bin")
        etag_b = nexus_fs.get_etag("/similar_b.bin")

        backend = nexus_fs.router.route("/").backend

        # Read manifests
        manifest_a = ChunkedReference.from_json(
            backend._transport._resolve(backend._blob_key(etag_a)).read_bytes()
        )
        manifest_b = ChunkedReference.from_json(
            backend._transport._resolve(backend._blob_key(etag_b)).read_bytes()
        )

        # Get chunk sets
        chunks_a = {c.chunk_hash for c in manifest_a.chunks}
        chunks_b = {c.chunk_hash for c in manifest_b.chunks}

        shared = chunks_a & chunks_b

        # Similar files should share chunks (due to CDC and common prefix)
        assert len(shared) > 0, "Similar files should share some chunks"

        # But not all chunks (different suffixes)
        assert len(chunks_a - chunks_b) > 0 or len(chunks_b - chunks_a) > 0

        # Verify both read correctly
        assert nexus_fs.sys_read("/similar_a.bin") == content_a
        assert nexus_fs.sys_read("/similar_b.bin") == content_b
