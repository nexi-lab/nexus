"""End-to-end tests for CDC Chunked Storage via FastAPI (Issue #1074)."""

import os

from nexus.backends.chunked_storage import CDC_THRESHOLD_BYTES


class TestChunkedStorageE2E:
    """End-to-end tests for chunked storage through NexusFS."""

    def test_small_file_not_chunked(self, nexus_fs) -> None:
        """Test that small files work normally (not chunked)."""
        content = b"This is a small test file content."

        # Write
        nexus_fs.write("/test_small.txt", content)

        # Read back
        read_content = nexus_fs.read("/test_small.txt")
        assert read_content == content

        # Verify not chunked
        etag = nexus_fs.get_etag("/test_small.txt")
        backend = nexus_fs.backend
        assert not backend._is_chunked_content(etag), "Small file should not be chunked"

    def test_large_file_chunked_write_read(self, nexus_fs) -> None:
        """Test that large files are chunked and can be read back correctly."""
        # Create content larger than CDC threshold (~17MB)
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        # Write
        nexus_fs.write("/test_large.bin", large_content)

        # Verify it was chunked
        etag = nexus_fs.get_etag("/test_large.bin")
        assert etag is not None

        backend = nexus_fs.backend
        assert backend._is_chunked_content(etag), "Large file should be chunked"

        # Read back
        read_content = nexus_fs.read("/test_large.bin")
        assert read_content == large_content, "Content mismatch after chunked read"

    def test_large_file_chunks_created(self, nexus_fs) -> None:
        """Test that individual chunks are created in CAS."""
        from nexus.backends.chunked_storage import ChunkedReference

        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        nexus_fs.write("/chunks_test.bin", large_content)

        etag = nexus_fs.get_etag("/chunks_test.bin")
        backend = nexus_fs.backend

        # Read manifest
        manifest_path = backend._hash_to_path(etag)
        manifest = ChunkedReference.from_json(manifest_path.read_bytes())

        # Verify each chunk exists
        for chunk_info in manifest.chunks:
            chunk_path = backend._hash_to_path(chunk_info.chunk_hash)
            assert chunk_path.exists(), f"Chunk {chunk_info.chunk_hash[:16]}... should exist"
            assert chunk_path.stat().st_size == chunk_info.length

    def test_large_file_delete_cleans_chunks(self, nexus_fs) -> None:
        """Test that deleting chunked files cleans up chunks."""
        from nexus.backends.chunked_storage import ChunkedReference

        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        # Write
        nexus_fs.write("/delete_test.bin", large_content)
        content_hash = nexus_fs.get_etag("/delete_test.bin")

        backend = nexus_fs.backend

        # Get chunk hashes before delete
        manifest = ChunkedReference.from_json(backend._hash_to_path(content_hash).read_bytes())
        chunk_hashes = [c.chunk_hash for c in manifest.chunks]

        # Verify chunks exist
        for ch in chunk_hashes:
            assert backend._hash_to_path(ch).exists()

        # Delete
        nexus_fs.delete("/delete_test.bin")

        # Verify file is gone
        assert nexus_fs.get_metadata("/delete_test.bin") is None

        # Verify chunks are deleted (ref_count was 1)
        for ch in chunk_hashes:
            assert not backend._hash_to_path(ch).exists(), f"Chunk {ch[:16]}... should be deleted"

    def test_file_size_correct_for_chunked(self, nexus_fs) -> None:
        """Test that file metadata shows original size, not manifest size."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 2_000_000)
        original_size = len(large_content)

        nexus_fs.write("/size_test.bin", large_content)

        metadata = nexus_fs.get_metadata("/size_test.bin")
        assert metadata["size"] == original_size

    def test_chunked_deduplication(self, nexus_fs) -> None:
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
        assert nexus_fs.read("/dedup_a.bin") == large_content
        assert nexus_fs.read("/dedup_b.bin") == large_content

        # Delete one, other should still work (ref_count)
        nexus_fs.delete("/dedup_a.bin")
        assert nexus_fs.read("/dedup_b.bin") == large_content


class TestChunkedStorageBackwardCompatibility:
    """Test backward compatibility with existing single-blob storage."""

    def test_mixed_small_and_large_files(self, nexus_fs) -> None:
        """Test that small and large files coexist correctly."""
        small_content = b"Small file content"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        # Write both types
        nexus_fs.write("/small.txt", small_content)
        nexus_fs.write("/large.bin", large_content)

        backend = nexus_fs.backend

        # Verify small not chunked, large chunked
        small_etag = nexus_fs.get_etag("/small.txt")
        large_etag = nexus_fs.get_etag("/large.bin")

        assert not backend._is_chunked_content(small_etag)
        assert backend._is_chunked_content(large_etag)

        # Read both
        assert nexus_fs.read("/small.txt") == small_content
        assert nexus_fs.read("/large.bin") == large_content

        # List directory
        files = nexus_fs.list("/")
        assert "/small.txt" in files or "small.txt" in files
        assert "/large.bin" in files or "large.bin" in files

        # Delete both
        nexus_fs.delete("/small.txt")
        nexus_fs.delete("/large.bin")

        # Verify deleted
        assert nexus_fs.get_metadata("/small.txt") is None
        assert nexus_fs.get_metadata("/large.bin") is None

    def test_overwrite_small_with_large(self, nexus_fs) -> None:
        """Test overwriting a small file with a large chunked file."""
        small_content = b"Initial small content"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)

        # Write small
        nexus_fs.write("/overwrite.bin", small_content)
        etag1 = nexus_fs.get_etag("/overwrite.bin")

        backend = nexus_fs.backend
        assert not backend._is_chunked_content(etag1)

        # Overwrite with large
        nexus_fs.write("/overwrite.bin", large_content)
        etag2 = nexus_fs.get_etag("/overwrite.bin")

        assert backend._is_chunked_content(etag2)
        assert nexus_fs.read("/overwrite.bin") == large_content

    def test_overwrite_large_with_small(self, nexus_fs) -> None:
        """Test overwriting a large chunked file with a small file."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)
        small_content = b"New small content"

        # Write large
        nexus_fs.write("/overwrite2.bin", large_content)
        etag1 = nexus_fs.get_etag("/overwrite2.bin")

        backend = nexus_fs.backend
        assert backend._is_chunked_content(etag1)

        # Overwrite with small
        nexus_fs.write("/overwrite2.bin", small_content)
        etag2 = nexus_fs.get_etag("/overwrite2.bin")

        assert not backend._is_chunked_content(etag2)
        assert nexus_fs.read("/overwrite2.bin") == small_content


class TestChunkedStorageCDCBehavior:
    """Tests for CDC-specific behavior."""

    def test_similar_files_share_chunks(self, nexus_fs) -> None:
        """Test that similar files (with common prefix) share some chunks."""
        from nexus.backends.chunked_storage import ChunkedReference

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

        backend = nexus_fs.backend

        # Read manifests
        manifest_a = ChunkedReference.from_json(backend._hash_to_path(etag_a).read_bytes())
        manifest_b = ChunkedReference.from_json(backend._hash_to_path(etag_b).read_bytes())

        # Get chunk sets
        chunks_a = {c.chunk_hash for c in manifest_a.chunks}
        chunks_b = {c.chunk_hash for c in manifest_b.chunks}

        shared = chunks_a & chunks_b

        # Similar files should share chunks (due to CDC and common prefix)
        assert len(shared) > 0, "Similar files should share some chunks"

        # But not all chunks (different suffixes)
        assert len(chunks_a - chunks_b) > 0 or len(chunks_b - chunks_a) > 0

        # Verify both read correctly
        assert nexus_fs.read("/similar_a.bin") == content_a
        assert nexus_fs.read("/similar_b.bin") == content_b
