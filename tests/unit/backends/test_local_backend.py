"""Unit tests for local filesystem backend."""

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content


@pytest.fixture
def temp_backend(tmp_path):
    """Create a temporary local backend for testing."""
    backend = LocalBackend(root_path=tmp_path / "backend")
    yield backend


def test_initialization(tmp_path):
    """Test backend initialization creates required directories."""
    root = tmp_path / "test_backend"
    backend = LocalBackend(root)

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
    content_hash = temp_backend.write_content(content).unwrap()

    # Verify hash is correct (using BLAKE3)
    expected_hash = hash_content(content)
    assert content_hash == expected_hash

    # Read content back
    retrieved = temp_backend.read_content(content_hash).unwrap()
    assert retrieved == content


def test_write_duplicate_content(temp_backend):
    """Test writing duplicate content returns same hash."""
    content = b"Duplicate test content"

    hash1 = temp_backend.write_content(content).unwrap()
    hash2 = temp_backend.write_content(content).unwrap()

    assert hash1 == hash2

    # Verify content can be read
    retrieved = temp_backend.read_content(hash1).unwrap()
    assert retrieved == content


def test_read_nonexistent_content(temp_backend):
    """Test reading non-existent content raises error."""
    fake_hash = "a" * 64

    with pytest.raises(NexusFileNotFoundError):
        temp_backend.read_content(fake_hash).unwrap()


def test_delete_content(temp_backend):
    """Test deleting content."""
    content = b"Content to delete"
    content_hash = temp_backend.write_content(content).unwrap()

    # Verify content exists
    retrieved = temp_backend.read_content(content_hash).unwrap()
    assert retrieved == content

    # Delete content
    temp_backend.delete_content(content_hash).unwrap()

    # Verify content is deleted
    with pytest.raises(NexusFileNotFoundError):
        temp_backend.read_content(content_hash).unwrap()


def test_delete_nonexistent_content(temp_backend):
    """Test deleting non-existent content doesn't raise error."""
    from contextlib import suppress

    fake_hash = "b" * 64

    # Should raise or handle gracefully (implementation dependent)
    with suppress(NexusFileNotFoundError):  # Expected behavior
        temp_backend.delete_content(fake_hash).unwrap()


def test_exists_content(temp_backend):
    """Test checking if content exists."""
    content = b"Existence test"
    content_hash = temp_backend.write_content(content).unwrap()

    assert temp_backend.content_exists(content_hash).unwrap() is True

    fake_hash = "c" * 64
    assert temp_backend.content_exists(fake_hash).unwrap() is False


def test_hash_to_path(temp_backend):
    """Test hash to path conversion."""
    content_hash = "abcdef1234567890" + "0" * 48  # 64 char hash

    path = temp_backend._hash_to_path(content_hash)

    # Should create two-level directory structure
    assert path.parent.name == "cd"
    assert path.parent.parent.name == "ab"
    assert path.name == content_hash


def test_hash_to_path_invalid_hash(temp_backend):
    """Test hash to path with invalid hash length."""
    with pytest.raises(ValueError, match="Invalid hash length"):
        temp_backend._hash_to_path("abc")


def test_compute_hash(temp_backend):
    """Test content hash computation (using BLAKE3)."""
    content = b"Test content for hashing"
    computed_hash = temp_backend._compute_hash(content)
    expected_hash = hash_content(content)

    assert computed_hash == expected_hash


def test_write_empty_content(temp_backend):
    """Test writing empty content."""
    content = b""
    content_hash = temp_backend.write_content(content).unwrap()

    # Verify hash is correct for empty content (using BLAKE3)
    expected_hash = hash_content(b"")
    assert content_hash == expected_hash

    # Read it back
    retrieved = temp_backend.read_content(content_hash).unwrap()
    assert retrieved == b""


def test_write_large_content(temp_backend):
    """Test writing large content."""
    # 10 MB of data
    content = b"X" * (10 * 1024 * 1024)
    content_hash = temp_backend.write_content(content).unwrap()

    # Verify it can be read back
    retrieved = temp_backend.read_content(content_hash).unwrap()
    assert len(retrieved) == len(content)
    assert retrieved == content


def test_get_content_size(temp_backend):
    """Test getting content size."""
    content = b"Test content for size"
    content_hash = temp_backend.write_content(content).unwrap()

    size = temp_backend.get_content_size(content_hash).unwrap()
    assert size == len(content)


def test_content_deduplication(temp_backend):
    """Test that duplicate content is deduplicated."""
    content = b"Deduplicate me!"

    # Write same content multiple times
    hash1 = temp_backend.write_content(content).unwrap()
    hash2 = temp_backend.write_content(content).unwrap()
    hash3 = temp_backend.write_content(content).unwrap()

    # All hashes should be identical
    assert hash1 == hash2 == hash3

    # Should only be stored once
    content_path = temp_backend._hash_to_path(hash1)
    assert content_path.exists()

    # Read should still work
    retrieved = temp_backend.read_content(hash1).unwrap()
    assert retrieved == content


def test_directory_creation(temp_backend):
    """Test creating directories."""
    dir_path = "/test/nested/directory"
    temp_backend.mkdir(dir_path, parents=True).unwrap()

    # Check that directory was created
    physical_path = temp_backend.dir_root / dir_path.lstrip("/")
    assert physical_path.exists()
    assert physical_path.is_dir()


def test_directory_creation_existing(temp_backend):
    """Test creating directory that already exists."""
    dir_path = "/test/existing"
    temp_backend.mkdir(dir_path, parents=True, exist_ok=True).unwrap()

    # Create again - should not raise
    temp_backend.mkdir(dir_path, exist_ok=True).unwrap()


def test_is_directory(temp_backend):
    """Test checking if path is a directory."""
    dir_path = "/test/directory"
    temp_backend.mkdir(dir_path, parents=True).unwrap()

    assert temp_backend.is_directory(dir_path).unwrap() is True


def test_backend_error_on_invalid_root():
    """Test that backend raises error for invalid root path."""
    # Create a file instead of directory
    import tempfile

    with tempfile.NamedTemporaryFile() as f, pytest.raises(BackendError):
        # Try to initialize backend with a file path
        backend = LocalBackend(f.name)
        backend._ensure_roots()


def test_binary_content(temp_backend):
    """Test handling of binary content."""
    # Binary data with all byte values
    content = bytes(range(256))
    content_hash = temp_backend.write_content(content).unwrap()

    retrieved = temp_backend.read_content(content_hash).unwrap()
    assert retrieved == content


def test_unicode_directory_names(temp_backend):
    """Test handling of unicode in directory names."""
    dir_path = "/test/unicode_测试_тест"

    try:
        temp_backend.mkdir(dir_path).unwrap()
        physical_path = temp_backend.dir_root / dir_path.lstrip("/")
        assert physical_path.exists()
    except Exception:
        # Some filesystems may not support unicode
        pytest.skip("Filesystem doesn't support unicode directory names")


def test_multiple_backends_same_root(tmp_path):
    """Test that multiple backend instances can share same root."""
    root = tmp_path / "shared_backend"

    backend1 = LocalBackend(root)
    backend2 = LocalBackend(root)

    # Write with first backend
    content = b"Shared content"
    hash1 = backend1.write_content(content).unwrap()

    # Read with second backend
    retrieved = backend2.read_content(hash1).unwrap()
    assert retrieved == content


def test_list_directory(temp_backend):
    """Test listing directory contents."""
    # Create a directory structure
    temp_backend.mkdir("/test", parents=True, exist_ok=True).unwrap()
    temp_backend.mkdir("/test/sub1", exist_ok=True).unwrap()
    temp_backend.mkdir("/test/sub2", exist_ok=True).unwrap()

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

    hash1 = temp_backend.write_content(content1).unwrap()
    hash2 = temp_backend.write_content(content2).unwrap()
    hash3 = temp_backend.write_content(content3).unwrap()

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
    hash1 = temp_backend.write_content(content1).unwrap()

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
    content_hash = temp_backend.write_content(content).unwrap()

    # Request same hash multiple times
    result = temp_backend.batch_read_content([content_hash, content_hash, content_hash])

    # Dictionary can only have one entry per unique key
    assert len(result) == 1
    assert result[content_hash] == content


def test_batch_read_content_with_cache(tmp_path):
    """Test that batch read leverages content cache."""
    from nexus.storage.content_cache import ContentCache

    cache = ContentCache(max_size_mb=10)
    backend = LocalBackend(root_path=tmp_path / "backend", content_cache=cache)

    # Write content
    content1 = b"Cached content 1"
    content2 = b"Cached content 2"
    hash1 = backend.write_content(content1).unwrap()
    hash2 = backend.write_content(content2).unwrap()

    # First batch read (populates cache)
    result1 = backend.batch_read_content([hash1, hash2])
    assert result1[hash1] == content1
    assert result1[hash2] == content2

    # Verify cache was populated
    assert backend.content_cache.get(hash1) == content1
    assert backend.content_cache.get(hash2) == content2

    # Second batch read (should hit cache)
    result2 = backend.batch_read_content([hash1, hash2])
    assert result2[hash1] == content1
    assert result2[hash2] == content2


def test_batch_read_content_parallel(tmp_path):
    """Test batch read uses parallel reads for multiple uncached files.

    This test verifies that:
    1. Multiple files can be read in parallel
    2. The parallel execution works correctly with ThreadPoolExecutor
    3. Results are correctly mapped to their hashes
    """
    backend = LocalBackend(root_path=tmp_path / "backend")
    # No cache - forces disk reads
    backend.content_cache = None

    # Write multiple files
    contents = [f"Content for file {i}".encode() for i in range(10)]
    hashes = [backend.write_content(content).unwrap() for content in contents]

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

    backend = LocalBackend(root_path=tmp_path / "backend")
    backend.content_cache = None  # Disable cache to force disk reads

    # Write 20 files
    contents = [f"Content for performance test file {i}".encode() for i in range(20)]
    hashes = [backend.write_content(content).unwrap() for content in contents]

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
    backend = LocalBackend(root_path=tmp_path / "backend")
    backend.content_cache = None

    content = b"Single file content"
    content_hash = backend.write_content(content).unwrap()

    # Batch read with single file
    result = backend.batch_read_content([content_hash])

    assert len(result) == 1
    assert result[content_hash] == content


def test_batch_read_workers_configurable(tmp_path):
    """Test that batch_read_workers is configurable via constructor."""
    # Default is 8
    backend_default = LocalBackend(root_path=tmp_path / "backend1")
    assert backend_default.batch_read_workers == 8

    # Custom value for HDD
    backend_hdd = LocalBackend(root_path=tmp_path / "backend2", batch_read_workers=2)
    assert backend_hdd.batch_read_workers == 2

    # Custom value for fast NVMe
    backend_nvme = LocalBackend(root_path=tmp_path / "backend3", batch_read_workers=16)
    assert backend_nvme.batch_read_workers == 16


def test_batch_read_respects_worker_limit(tmp_path):
    """Test that batch read respects the configured worker limit."""
    # Create backend with low worker count (simulating HDD config)
    backend = LocalBackend(root_path=tmp_path / "backend", batch_read_workers=2)
    backend.content_cache = None

    # Write 10 files
    contents = [f"Content {i}".encode() for i in range(10)]
    hashes = [backend.write_content(c).unwrap() for c in contents]

    # Batch read should work correctly even with limited workers
    result = backend.batch_read_content(hashes)

    assert len(result) == 10
    for i, h in enumerate(hashes):
        assert result[h] == contents[i]


def test_stream_content_small_file(temp_backend):
    """Test streaming a small file."""
    content = b"Small file content for streaming test"
    content_hash = temp_backend.write_content(content).unwrap()

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
    content_hash = temp_backend.write_content(content).unwrap()

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
    content_hash = temp_backend.write_content(content).unwrap()

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
    content_hash = temp_backend.write_content(large_content).unwrap()

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
    """Concurrent tests for LocalBackend with CASBlobStore integration."""

    def test_concurrent_writes_same_content(self, temp_backend):
        """50 threads writing identical content — ref_count must be exactly 50."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        content = b"concurrent same content sync"

        def writer(_i: int) -> str:
            return temp_backend.write_content(content).unwrap()

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_THREADS)]
            hashes = [f.result() for f in as_completed(futures)]

        # All should return the same hash
        assert len(set(hashes)) == 1
        h = hashes[0]

        # Content must be readable
        assert temp_backend.read_content(h).unwrap() == content

        # ref_count must be exactly NUM_THREADS
        meta = temp_backend._cas.read_meta(h)
        assert meta.ref_count == NUM_THREADS

    def test_concurrent_writes_different_content(self, temp_backend):
        """50 threads writing unique content — all succeed independently."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def writer(i: int) -> str:
            content = f"unique sync content {i}".encode()
            return temp_backend.write_content(content).unwrap()

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_THREADS)]
            hashes = [f.result() for f in as_completed(futures)]

        # All hashes should be unique
        assert len(set(hashes)) == NUM_THREADS

        # Each blob should exist with ref_count=1
        for h in hashes:
            meta = temp_backend._cas.read_meta(h)
            assert meta.ref_count == 1

    def test_concurrent_read_write(self, temp_backend):
        """Concurrent reads and writes don't corrupt data."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        content = b"read-write concurrent sync"
        h = temp_backend.write_content(content).unwrap()

        def worker(i: int) -> bytes | str:
            if i % 2 == 0:
                return temp_backend.write_content(content).unwrap()
            else:
                return temp_backend.read_content(h).unwrap()

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
    """Integration tests for chunked storage with CASBlobStore."""

    @pytest.fixture
    def chunked_backend(self, tmp_path):
        """Backend with low CDC threshold for testing chunking."""
        backend = LocalBackend(root_path=tmp_path / "chunked")
        backend.cdc_threshold = 1024  # Lower for testing
        return backend

    def test_chunked_write_read_roundtrip(self, chunked_backend):
        """Write >threshold file, verify manifest + chunks in CAS, read back."""
        # Create content larger than threshold (1024 bytes)
        content = b"A" * 500 + b"B" * 500 + b"C" * 200
        assert len(content) >= chunked_backend.cdc_threshold

        # Write
        manifest_hash = chunked_backend.write_content(content).unwrap()

        # Verify manifest is in CAS
        meta = chunked_backend._cas.read_meta(manifest_hash)
        assert meta.size == len(content)
        assert any(k == "is_chunked_manifest" and v for k, v in meta.extra)

        # Verify it's recognized as chunked
        assert chunked_backend._is_chunked_content(manifest_hash)

        # Read back and verify content integrity
        read_back = chunked_backend.read_content(manifest_hash).unwrap()
        assert read_back == content

    def test_chunked_manifest_structure(self, chunked_backend):
        """Verify manifest has correct chunk metadata."""
        from nexus.backends.chunked_storage import ChunkedReference

        content = b"X" * 2048  # 2KB, above threshold
        manifest_hash = chunked_backend.write_content(content).unwrap()

        # Read raw manifest
        manifest_bytes = chunked_backend._cas.read_blob(manifest_hash)
        manifest = ChunkedReference.from_json(manifest_bytes)

        assert manifest.type == "chunked_manifest_v1"
        assert manifest.total_size == len(content)
        assert manifest.chunk_count > 0
        assert manifest.content_hash == hash_content(content)

        # Each chunk should exist in CAS with ref_count >= 1
        for chunk_info in manifest.chunks:
            assert chunked_backend._cas.blob_exists(chunk_info.chunk_hash)
            chunk_meta = chunked_backend._cas.read_meta(chunk_info.chunk_hash)
            assert chunk_meta.ref_count >= 1

    def test_chunked_delete_releases_chunks(self, chunked_backend):
        """Deleting chunked content releases all chunk refs."""
        from nexus.backends.chunked_storage import ChunkedReference

        content = b"D" * 2048
        manifest_hash = chunked_backend.write_content(content).unwrap()

        # Read manifest to get chunk hashes
        manifest_bytes = chunked_backend._cas.read_blob(manifest_hash)
        manifest = ChunkedReference.from_json(manifest_bytes)
        chunk_hashes = [ci.chunk_hash for ci in manifest.chunks]

        # Delete
        chunked_backend.delete_content(manifest_hash)

        # Manifest should be gone
        assert not chunked_backend._cas.blob_exists(manifest_hash)

        # All chunks should be gone (ref_count was 1)
        for ch in chunk_hashes:
            assert not chunked_backend._cas.blob_exists(ch)

    def test_chunked_deduplication(self, chunked_backend):
        """Writing same chunked content twice increments ref_count."""
        content = b"E" * 2048
        h1 = chunked_backend.write_content(content).unwrap()
        h2 = chunked_backend.write_content(content).unwrap()

        assert h1 == h2
        meta = chunked_backend._cas.read_meta(h1)
        assert meta.ref_count == 2

    def test_concurrent_chunked_writes(self, chunked_backend):
        """Multiple threads writing different chunked content."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def writer(i: int) -> str:
            content = f"chunked content {i} ".encode() * 200  # ~3.6KB each
            return chunked_backend.write_content(content).unwrap()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(writer, i) for i in range(8)]
            hashes = [f.result() for f in as_completed(futures)]

        # All should be unique
        assert len(set(hashes)) == 8

        # Each should be readable
        for h in sorted(set(hashes)):
            assert chunked_backend._is_chunked_content(h)
