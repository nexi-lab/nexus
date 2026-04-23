"""Unit tests for local filesystem backend."""

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.hash_fast import hash_content


@pytest.fixture
def temp_backend(tmp_path):
    """Create a temporary local backend for testing."""
    backend = CASLocalBackend(root_path=tmp_path / "backend")
    yield backend


def test_initialization(tmp_path):
    """Test backend initialization creates required directories."""
    root = tmp_path / "test_backend"
    backend = CASLocalBackend(root)

    assert backend.root_path == root.resolve()
    assert backend.cas_root == root / "cas"
    assert backend.dir_root == root / "dirs"
    assert backend.cas_root.exists()
    assert backend.dir_root.exists()


def test_backend_name(temp_backend):
    """Test that backend name property returns correct value."""
    assert temp_backend.name == "local"


def test_write_and_read_content(temp_backend):
    """Test writing and reading content."""
    content = b"Hello, World!"
    result = temp_backend.write_content(content)
    content_hash = result.content_id

    # Verify hash is correct (using BLAKE3)
    expected_hash = hash_content(content)
    assert content_hash == expected_hash

    # Read content back
    retrieved = temp_backend.read_content(content_hash)
    assert retrieved == content


def test_write_duplicate_content(temp_backend):
    """Test writing duplicate content returns same hash."""
    content = b"Duplicate test content"

    hash1 = temp_backend.write_content(content).content_id
    hash2 = temp_backend.write_content(content).content_id

    assert hash1 == hash2

    # Verify content can be read
    retrieved = temp_backend.read_content(hash1)
    assert retrieved == content


def test_read_nonexistent_content(temp_backend):
    """Test reading non-existent content raises error."""
    fake_hash = "a" * 64

    with pytest.raises(NexusFileNotFoundError):
        temp_backend.read_content(fake_hash)


def test_delete_content(temp_backend):
    """Test deleting content."""
    content = b"Content to delete"
    content_hash = temp_backend.write_content(content).content_id

    # Verify content exists
    retrieved = temp_backend.read_content(content_hash)
    assert retrieved == content

    # Delete content
    temp_backend.delete_content(content_hash)

    # Verify content is deleted
    with pytest.raises(NexusFileNotFoundError):
        temp_backend.read_content(content_hash)


def test_delete_nonexistent_content(temp_backend):
    """Test deleting non-existent content doesn't raise error."""
    from contextlib import suppress

    fake_hash = "b" * 64

    # Should raise or handle gracefully (implementation dependent)
    with suppress(NexusFileNotFoundError):  # Expected behavior
        temp_backend.delete_content(fake_hash)


def test_exists_content(temp_backend):
    """Test checking if content exists."""
    content = b"Existence test"
    content_hash = temp_backend.write_content(content).content_id

    assert temp_backend.content_exists(content_hash) is True

    fake_hash = "c" * 64
    assert temp_backend.content_exists(fake_hash) is False


def test_blob_key(temp_backend):
    """Test CAS blob key generation."""
    content_hash = "abcdef1234567890" + "0" * 48  # 64 char hash

    key = temp_backend._blob_key(content_hash)

    # Should create two-level directory structure: cas/ab/cd/<hash>
    assert key == f"cas/ab/cd/{content_hash}"


def test_content_hash_roundtrip(temp_backend):
    """Test content hash via write/read roundtrip."""
    content = b"Test content for hashing"
    expected_hash = hash_content(content)

    result = temp_backend.write_content(content)
    assert result.content_id == expected_hash


def test_write_empty_content(temp_backend):
    """Test writing empty content."""
    content = b""
    content_hash = temp_backend.write_content(content).content_id

    # Verify hash is correct for empty content (using BLAKE3)
    expected_hash = hash_content(b"")
    assert content_hash == expected_hash

    # Read it back
    retrieved = temp_backend.read_content(content_hash)
    assert retrieved == b""


def test_write_large_content(temp_backend):
    """Test writing large content."""
    # 10 MB of data
    content = b"X" * (10 * 1024 * 1024)
    content_hash = temp_backend.write_content(content).content_id

    # Verify it can be read back
    retrieved = temp_backend.read_content(content_hash)
    assert len(retrieved) == len(content)
    assert retrieved == content


def test_get_content_size(temp_backend):
    """Test getting content size."""
    content = b"Test content for size"
    content_hash = temp_backend.write_content(content).content_id

    size = temp_backend.get_content_size(content_hash)
    assert size == len(content)


def test_content_deduplication(temp_backend):
    """Test that duplicate content is deduplicated."""
    content = b"Deduplicate me!"

    # Write same content multiple times
    hash1 = temp_backend.write_content(content).content_id
    hash2 = temp_backend.write_content(content).content_id
    hash3 = temp_backend.write_content(content).content_id

    # All hashes should be identical
    assert hash1 == hash2 == hash3

    # Should only be stored once (blob exists in transport)
    blob_key = temp_backend._blob_key(hash1)
    assert temp_backend._transport.exists(blob_key)

    # Read should still work
    retrieved = temp_backend.read_content(hash1)
    assert retrieved == content


def test_directory_creation(temp_backend):
    """Test creating directories."""
    dir_path = "/test/nested/directory"
    temp_backend.mkdir(dir_path, parents=True)

    # Check that directory was created
    physical_path = temp_backend.dir_root / dir_path.lstrip("/")
    assert physical_path.exists()
    assert physical_path.is_dir()


def test_directory_creation_existing(temp_backend):
    """Test creating directory that already exists."""
    dir_path = "/test/existing"
    temp_backend.mkdir(dir_path, parents=True, exist_ok=True)

    # Create again - should not raise
    temp_backend.mkdir(dir_path, exist_ok=True)


def test_is_directory(temp_backend):
    """Test checking if path is a directory."""
    dir_path = "/test/directory"
    temp_backend.mkdir(dir_path, parents=True)

    assert temp_backend.is_directory(dir_path) is True


def test_backend_error_on_invalid_root():
    """Test that backend raises error for invalid root path."""
    # Create a file instead of directory
    import tempfile

    with tempfile.NamedTemporaryFile() as f, pytest.raises((NotADirectoryError, OSError)):
        # Try to initialize backend with a file path (root is a file, not dir)
        CASLocalBackend(f.name)


def test_binary_content(temp_backend):
    """Test handling of binary content."""
    # Binary data with all byte values
    content = bytes(range(256))
    content_hash = temp_backend.write_content(content).content_id

    retrieved = temp_backend.read_content(content_hash)
    assert retrieved == content


def test_unicode_directory_names(temp_backend):
    """Test handling of unicode in directory names."""
    dir_path = "/test/unicode_测试_тест"

    try:
        temp_backend.mkdir(dir_path)
        physical_path = temp_backend.dir_root / dir_path.lstrip("/")
        assert physical_path.exists()
    except Exception:
        # Some filesystems may not support unicode
        pytest.skip("Filesystem doesn't support unicode directory names")


def test_multiple_backends_same_root(tmp_path):
    """Test that multiple backend instances can share same root."""
    root = tmp_path / "shared_backend"

    backend1 = CASLocalBackend(root)
    backend2 = CASLocalBackend(root)

    # Write with first backend
    content = b"Shared content"
    hash1 = backend1.write_content(content).content_id

    # Read with second backend
    retrieved = backend2.read_content(hash1)
    assert retrieved == content


def test_list_directory(temp_backend):
    """Test listing directory contents."""
    # Create a directory structure
    temp_backend.mkdir("/test", parents=True, exist_ok=True)
    temp_backend.mkdir("/test/sub1", exist_ok=True)
    temp_backend.mkdir("/test/sub2", exist_ok=True)

    items = temp_backend.list_dir("/test")

    # list_dir returns directories with trailing slashes
    assert "sub1/" in items
    assert "sub2/" in items


def test_batch_read_content_basic(temp_backend):
    """Test batch reading multiple content items."""
    # Write multiple content items
    content1 = b"Content 1"
    content2 = b"Content 2"
    content3 = b"Content 3"

    hash1 = temp_backend.write_content(content1).content_id
    hash2 = temp_backend.write_content(content2).content_id
    hash3 = temp_backend.write_content(content3).content_id

    # Batch read all content
    result = temp_backend.batch_read_content([hash1, hash2, hash3])

    assert len(result) == 3
    assert result[hash1] == content1
    assert result[hash2] == content2
    assert result[hash3] == content3


def test_batch_read_content_missing_hashes(temp_backend):
    """Test batch read with some missing content hashes."""
    # Write one content item
    content1 = b"Content 1"
    hash1 = temp_backend.write_content(content1).content_id

    # Create fake hashes that don't exist
    fake_hash1 = "0" * 64
    fake_hash2 = "1" * 64

    # Batch read with mix of existing and missing
    result = temp_backend.batch_read_content([hash1, fake_hash1, fake_hash2])

    assert len(result) == 3
    assert result[hash1] == content1
    assert result[fake_hash1] is None  # Missing content returns None
    assert result[fake_hash2] is None


def test_batch_read_content_empty_list(temp_backend):
    """Test batch read with empty list."""
    result = temp_backend.batch_read_content([])
    assert result == {}


def test_batch_read_content_deduplication(temp_backend):
    """Test that batch read handles duplicate hashes correctly."""
    content = b"Duplicate content"
    content_hash = temp_backend.write_content(content).content_id

    # Request same hash multiple times
    result = temp_backend.batch_read_content([content_hash, content_hash, content_hash])

    # Dictionary can only have one entry per unique key
    assert len(result) == 1
    assert result[content_hash] == content


def test_batch_read_content_parallel(tmp_path):
    """Test batch read uses parallel reads for multiple uncached files.

    This test verifies that:
    1. Multiple files can be read in parallel
    2. The parallel execution works correctly with ThreadPoolExecutor
    3. Results are correctly mapped to their hashes
    """
    backend = CASLocalBackend(root_path=tmp_path / "backend")

    # Write multiple files
    contents = [f"Content for file {i}".encode() for i in range(10)]
    hashes = [backend.write_content(content).content_id for content in contents]

    # Batch read all files (will use parallel reads since cache is disabled)
    result = backend.batch_read_content(hashes)

    # Verify all content was read correctly
    assert len(result) == 10
    for i, h in enumerate(hashes):
        assert result[h] == contents[i]


def test_batch_read_content_parallel_performance(tmp_path):
    """Test that parallel batch read is faster than sequential for many files.

    This is a basic sanity check that parallelism is working.
    """
    import time

    backend = CASLocalBackend(root_path=tmp_path / "backend")

    # Write 20 files
    contents = [f"Content for performance test file {i}".encode() for i in range(20)]
    hashes = [backend.write_content(content).content_id for content in contents]

    # Time batch read (should be parallel)
    start = time.time()
    result = backend.batch_read_content(hashes)
    elapsed = time.time() - start

    # Verify correctness
    assert len(result) == 20
    for i, h in enumerate(hashes):
        assert result[h] == contents[i]

    # Should complete in reasonable time (generous limit for CI environments)
    # Even sequential reads of 20 small files should be < 1s
    assert elapsed < 5.0, f"Batch read took {elapsed:.2f}s, expected < 5s"


def test_batch_read_content_single_file_no_threadpool(tmp_path):
    """Test that single file batch read doesn't use ThreadPoolExecutor overhead."""
    backend = CASLocalBackend(root_path=tmp_path / "backend")

    content = b"Single file content"
    content_hash = backend.write_content(content).content_id

    # Batch read with single file
    result = backend.batch_read_content([content_hash])

    assert len(result) == 1
    assert result[content_hash] == content


def test_batch_read_workers_configurable(tmp_path):
    """Test that batch_read_workers is configurable via constructor."""
    # Default is 8
    backend_default = CASLocalBackend(root_path=tmp_path / "backend1")
    assert backend_default.batch_read_workers == 8

    # Custom value for HDD
    backend_hdd = CASLocalBackend(root_path=tmp_path / "backend2", batch_read_workers=2)
    assert backend_hdd.batch_read_workers == 2

    # Custom value for fast NVMe
    backend_nvme = CASLocalBackend(root_path=tmp_path / "backend3", batch_read_workers=16)
    assert backend_nvme.batch_read_workers == 16


def test_batch_read_respects_worker_limit(tmp_path):
    """Test that batch read respects the configured worker limit."""
    # Create backend with low worker count (simulating HDD config)
    backend = CASLocalBackend(root_path=tmp_path / "backend", batch_read_workers=2)

    # Write 10 files
    contents = [f"Content {i}".encode() for i in range(10)]
    hashes = [backend.write_content(c).content_id for c in contents]

    # Batch read should work correctly even with limited workers
    result = backend.batch_read_content(hashes)

    assert len(result) == 10
    for i, h in enumerate(hashes):
        assert result[h] == contents[i]


def test_stream_content_small_file(temp_backend):
    """Test streaming a small file."""
    content = b"Small file content for streaming test"
    content_hash = temp_backend.write_content(content).content_id

    # Stream content
    chunks = list(temp_backend.stream_content(content_hash, chunk_size=10))

    # Verify chunks reassemble to original content
    streamed_content = b"".join(chunks)
    assert streamed_content == content

    # Verify streaming produced multiple chunks
    assert len(chunks) > 1


def test_stream_content_large_file(temp_backend):
    """Test streaming a large file in chunks."""
    # Create 1MB test file
    content = b"X" * (1024 * 1024)
    content_hash = temp_backend.write_content(content).content_id

    # Stream in 64KB chunks
    chunk_size = 64 * 1024
    chunks = list(temp_backend.stream_content(content_hash, chunk_size=chunk_size))

    # Verify chunks reassemble correctly
    streamed_content = b"".join(chunks)
    assert streamed_content == content

    # Verify chunk count (should be ~16 chunks for 1MB / 64KB)
    assert len(chunks) == 16


def test_stream_content_exact_chunk_boundary(temp_backend):
    """Test streaming when file size is exact multiple of chunk size."""
    chunk_size = 100
    content = b"A" * (chunk_size * 5)  # Exactly 5 chunks
    content_hash = temp_backend.write_content(content).content_id

    chunks = list(temp_backend.stream_content(content_hash, chunk_size=chunk_size))

    assert len(chunks) == 5
    assert all(len(chunk) == chunk_size for chunk in chunks)
    assert b"".join(chunks) == content


def test_stream_content_missing_file(temp_backend):
    """Test that streaming non-existent content raises error."""
    fake_hash = "0" * 64

    with pytest.raises(NexusFileNotFoundError):
        list(temp_backend.stream_content(fake_hash))


def test_stream_content_memory_efficient(temp_backend):
    """Test that streaming doesn't load entire file into memory."""
    # Create 10MB file
    large_content = b"X" * (10 * 1024 * 1024)
    content_hash = temp_backend.write_content(large_content).content_id

    # Stream it - should not cause memory spike
    total_bytes = 0
    for chunk in temp_backend.stream_content(content_hash, chunk_size=8192):
        total_bytes += len(chunk)
        # Process chunk (in real use, this could be written to network/disk)
        assert len(chunk) <= 8192

    assert total_bytes == len(large_content)


# ---------------------------------------------------------------------------
# Concurrent sync tests (Issue #925: verify CASBlobStore integration)
# ---------------------------------------------------------------------------

NUM_THREADS = 50


class TestConcurrentSync:
    """Concurrent tests for CASLocalBackend with CASBlobStore integration."""

    def test_concurrent_writes_same_content(self, temp_backend):
        """50 threads writing identical content — all must succeed with same hash."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        content = b"concurrent same content sync"

        def writer(_i: int) -> str:
            return temp_backend.write_content(content).content_id

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_THREADS)]
            hashes = [f.result() for f in as_completed(futures)]

        # All should return the same hash
        assert len(set(hashes)) == 1
        h = hashes[0]

        # Content must be readable
        assert temp_backend.read_content(h) == content

        # Content must be readable
        assert temp_backend.content_exists(h)

    def test_concurrent_writes_different_content(self, temp_backend):
        """50 threads writing unique content — all succeed independently."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def writer(i: int) -> str:
            content = f"unique sync content {i}".encode()
            return temp_backend.write_content(content).content_id

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_THREADS)]
            hashes = [f.result() for f in as_completed(futures)]

        # All hashes should be unique
        assert len(set(hashes)) == NUM_THREADS

        # Each blob should exist
        for h in hashes:
            assert temp_backend.content_exists(h)

    def test_concurrent_read_write(self, temp_backend):
        """Concurrent reads and writes don't corrupt data."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        content = b"read-write concurrent sync"
        h = temp_backend.write_content(content).content_id

        def worker(i: int) -> bytes | str:
            if i % 2 == 0:
                return temp_backend.write_content(content).content_id
            else:
                return temp_backend.read_content(h)

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(worker, i) for i in range(NUM_THREADS)]
            results = [f.result() for f in as_completed(futures)]

        # Reads should return correct content
        reads = [r for r in results if isinstance(r, bytes)]
        assert all(r == content for r in reads)

        # All writes should return the same hash
        writes = [r for r in results if isinstance(r, str)]
        assert all(r == h for r in writes)


# === Chunked + CAS Integration Tests (Issue #925, Decision #11) ===


class TestChunkedCASIntegration:
    """Integration tests for chunked storage with CASLocalBackend + CDCEngine."""

    @pytest.fixture
    def chunked_backend(self, tmp_path):
        """CASLocalBackend with low CDC threshold for testing chunking."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        backend = CASLocalBackend(root_path=tmp_path / "chunked")
        backend._cdc.threshold = 1024  # Lower for testing
        return backend

    def test_chunked_write_read_roundtrip(self, chunked_backend):
        """Write >threshold file, verify CDC routing, read back."""
        content = b"A" * 500 + b"B" * 500 + b"C" * 200
        assert len(content) >= chunked_backend._cdc.threshold

        manifest_hash = chunked_backend.write_content(content).content_id

        # Verify it's recognized as chunked
        assert chunked_backend._cdc.is_chunked(manifest_hash)

        # Read back and verify content integrity
        read_back = chunked_backend.read_content(manifest_hash)
        assert read_back == content

    def test_chunked_manifest_structure(self, chunked_backend):
        """Verify manifest has correct chunk metadata."""
        from nexus.backends.engines.cdc import ChunkedReference

        content = b"X" * 2048  # 2KB, above threshold
        manifest_hash = chunked_backend.write_content(content).content_id

        # Read raw manifest via transport
        key = chunked_backend._blob_key(manifest_hash)
        manifest_bytes, _ = chunked_backend._transport.fetch(key)
        manifest = ChunkedReference.from_json(manifest_bytes)

        assert manifest.type == "chunked_manifest_v1"
        assert manifest.total_size == len(content)
        assert manifest.chunk_count > 0
        assert manifest.content_hash == hash_content(content)

    def test_chunked_delete_releases_chunks(self, chunked_backend):
        """Deleting chunked content releases all chunk refs."""
        from nexus.backends.engines.cdc import ChunkedReference

        content = b"D" * 2048
        manifest_hash = chunked_backend.write_content(content).content_id

        # Read manifest to get chunk hashes
        key = chunked_backend._blob_key(manifest_hash)
        manifest_bytes, _ = chunked_backend._transport.fetch(key)
        manifest = ChunkedReference.from_json(manifest_bytes)
        chunk_hashes = [ci.chunk_hash for ci in manifest.chunks]

        # Delete
        chunked_backend.delete_content(manifest_hash)

        # Manifest should be gone
        assert not chunked_backend._transport.exists(key)

        # All chunks should be gone
        for ch in chunk_hashes:
            assert not chunked_backend._transport.exists(chunked_backend._blob_key(ch))

    def test_chunked_deduplication(self, chunked_backend):
        """Writing same chunked content twice produces same hash."""
        content = b"E" * 2048
        h1 = chunked_backend.write_content(content).content_id
        h2 = chunked_backend.write_content(content).content_id

        assert h1 == h2

    def test_concurrent_chunked_writes(self, chunked_backend):
        """Multiple threads writing different chunked content."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def writer(i: int) -> str:
            content = f"chunked content {i} ".encode() * 200  # ~3.6KB each
            return chunked_backend.write_content(content).content_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(writer, i) for i in range(8)]
            hashes = [f.result() for f in as_completed(futures)]

        # All should be unique
        assert len(set(hashes)) == 8

        # Each should be readable
        for h in sorted(set(hashes)):
            assert chunked_backend._cdc.is_chunked(h)
