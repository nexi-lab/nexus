"""Unit tests for async local filesystem backend (Phase 2).

Tests use PostgreSQL for consistency with Phase 1 AsyncMetadataStore tests.
Follows TDD approach: write failing tests first, then implement.
"""

import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# These imports will fail until we implement AsyncLocalBackend
from nexus.backends.async_local import AsyncLocalBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content


# === Fixtures ===


@pytest_asyncio.fixture
async def temp_backend(tmp_path: Path) -> AsyncGenerator[AsyncLocalBackend, None]:
    """Create a temporary async local backend for testing."""
    backend = AsyncLocalBackend(root_path=tmp_path / "backend")
    await backend.initialize()
    yield backend
    await backend.close()


@pytest_asyncio.fixture
async def backend_with_cache(tmp_path: Path) -> AsyncGenerator[AsyncLocalBackend, None]:
    """Create a temporary async local backend with content cache enabled."""
    from nexus.storage.content_cache import ContentCache

    cache = ContentCache(max_size_mb=10)
    backend = AsyncLocalBackend(root_path=tmp_path / "backend", content_cache=cache)
    await backend.initialize()
    yield backend
    await backend.close()


# === Initialization Tests ===


@pytest.mark.asyncio
async def test_initialization(tmp_path: Path) -> None:
    """Test backend initialization creates required directories."""
    root = tmp_path / "test_backend"
    backend = AsyncLocalBackend(root)
    await backend.initialize()

    assert backend.root_path == root.resolve()
    assert backend.cas_root == root / "cas"
    assert backend.dir_root == root / "dirs"
    assert backend.cas_root.exists()
    assert backend.dir_root.exists()

    await backend.close()


@pytest.mark.asyncio
async def test_backend_name(temp_backend: AsyncLocalBackend) -> None:
    """Test that backend name property returns correct value."""
    assert temp_backend.name == "local"


# === Content Write/Read Tests ===


@pytest.mark.asyncio
async def test_write_and_read_content(temp_backend: AsyncLocalBackend) -> None:
    """Test writing and reading content asynchronously."""
    content = b"Hello, World!"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Verify hash is correct (using BLAKE3)
    expected_hash = hash_content(content)
    assert content_hash == expected_hash

    # Read content back
    read_result = await temp_backend.read_content(content_hash)
    retrieved = read_result.unwrap()
    assert retrieved == content


@pytest.mark.asyncio
async def test_write_duplicate_content(temp_backend: AsyncLocalBackend) -> None:
    """Test writing duplicate content returns same hash and increments ref count."""
    content = b"Duplicate test content"

    result1 = await temp_backend.write_content(content)
    hash1 = result1.unwrap()
    result2 = await temp_backend.write_content(content)
    hash2 = result2.unwrap()

    assert hash1 == hash2

    # Verify content can be read
    read_result = await temp_backend.read_content(hash1)
    retrieved = read_result.unwrap()
    assert retrieved == content

    # Verify ref count was incremented
    ref_result = await temp_backend.get_ref_count(hash1)
    ref_count = ref_result.unwrap()
    assert ref_count == 2


@pytest.mark.asyncio
async def test_read_nonexistent_content(temp_backend: AsyncLocalBackend) -> None:
    """Test reading non-existent content raises error."""
    fake_hash = "a" * 64

    with pytest.raises(NexusFileNotFoundError):
        result = await temp_backend.read_content(fake_hash)
        result.unwrap()


@pytest.mark.asyncio
async def test_write_empty_content(temp_backend: AsyncLocalBackend) -> None:
    """Test writing empty content."""
    content = b""
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Verify hash is correct for empty content (using BLAKE3)
    expected_hash = hash_content(b"")
    assert content_hash == expected_hash

    # Read it back
    read_result = await temp_backend.read_content(content_hash)
    retrieved = read_result.unwrap()
    assert retrieved == b""


@pytest.mark.asyncio
async def test_write_large_content(temp_backend: AsyncLocalBackend) -> None:
    """Test writing large content (below CDC threshold)."""
    # 10 MB of data (below default 16MB CDC threshold)
    content = b"X" * (10 * 1024 * 1024)
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Verify it can be read back
    read_result = await temp_backend.read_content(content_hash)
    retrieved = read_result.unwrap()
    assert len(retrieved) == len(content)
    assert retrieved == content


@pytest.mark.asyncio
async def test_binary_content(temp_backend: AsyncLocalBackend) -> None:
    """Test handling of binary content with all byte values."""
    content = bytes(range(256))
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    read_result = await temp_backend.read_content(content_hash)
    retrieved = read_result.unwrap()
    assert retrieved == content


# === Delete Tests ===


@pytest.mark.asyncio
async def test_delete_content(temp_backend: AsyncLocalBackend) -> None:
    """Test deleting content with reference counting."""
    content = b"Content to delete"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Verify content exists
    read_result = await temp_backend.read_content(content_hash)
    retrieved = read_result.unwrap()
    assert retrieved == content

    # Delete content
    delete_result = await temp_backend.delete_content(content_hash)
    delete_result.unwrap()

    # Verify content is deleted
    with pytest.raises(NexusFileNotFoundError):
        read_result = await temp_backend.read_content(content_hash)
        read_result.unwrap()


@pytest.mark.asyncio
async def test_delete_with_multiple_references(temp_backend: AsyncLocalBackend) -> None:
    """Test that content with multiple references decrements ref count instead of deleting."""
    content = b"Content with multiple refs"

    # Write same content twice to create ref_count=2
    result1 = await temp_backend.write_content(content)
    hash1 = result1.unwrap()
    await temp_backend.write_content(content)

    # Verify ref count is 2
    ref_result = await temp_backend.get_ref_count(hash1)
    assert ref_result.unwrap() == 2

    # Delete once - should decrement ref count
    await temp_backend.delete_content(hash1)

    # Content should still exist with ref_count=1
    read_result = await temp_backend.read_content(hash1)
    assert read_result.unwrap() == content

    ref_result = await temp_backend.get_ref_count(hash1)
    assert ref_result.unwrap() == 1

    # Delete again - should remove content
    await temp_backend.delete_content(hash1)

    with pytest.raises(NexusFileNotFoundError):
        result = await temp_backend.read_content(hash1)
        result.unwrap()


@pytest.mark.asyncio
async def test_delete_nonexistent_content(temp_backend: AsyncLocalBackend) -> None:
    """Test deleting non-existent content returns not found."""
    fake_hash = "b" * 64

    result = await temp_backend.delete_content(fake_hash)
    assert not result.success


# === Content Exists Tests ===


@pytest.mark.asyncio
async def test_content_exists(temp_backend: AsyncLocalBackend) -> None:
    """Test checking if content exists."""
    content = b"Existence test"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    exists_result = await temp_backend.content_exists(content_hash)
    assert exists_result.unwrap() is True

    fake_hash = "c" * 64
    exists_result = await temp_backend.content_exists(fake_hash)
    assert exists_result.unwrap() is False


@pytest.mark.asyncio
async def test_content_exists_after_delete(temp_backend: AsyncLocalBackend) -> None:
    """Test content_exists returns False after deletion."""
    content = b"Delete and check existence"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Initially exists
    exists_result = await temp_backend.content_exists(content_hash)
    assert exists_result.unwrap() is True

    # Delete it
    await temp_backend.delete_content(content_hash)

    # Should no longer exist
    exists_result = await temp_backend.content_exists(content_hash)
    assert exists_result.unwrap() is False


# === Content Size Tests ===


@pytest.mark.asyncio
async def test_get_content_size(temp_backend: AsyncLocalBackend) -> None:
    """Test getting content size."""
    content = b"Test content for size"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    size_result = await temp_backend.get_content_size(content_hash)
    size = size_result.unwrap()
    assert size == len(content)


@pytest.mark.asyncio
async def test_get_content_size_nonexistent(temp_backend: AsyncLocalBackend) -> None:
    """Test getting size of non-existent content."""
    fake_hash = "d" * 64

    result = await temp_backend.get_content_size(fake_hash)
    assert not result.success


# === Reference Count Tests ===


@pytest.mark.asyncio
async def test_get_ref_count(temp_backend: AsyncLocalBackend) -> None:
    """Test getting reference count."""
    content = b"Ref count test"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Initial ref count should be 1
    ref_result = await temp_backend.get_ref_count(content_hash)
    assert ref_result.unwrap() == 1

    # Write again - ref count should increase
    await temp_backend.write_content(content)
    ref_result = await temp_backend.get_ref_count(content_hash)
    assert ref_result.unwrap() == 2


@pytest.mark.asyncio
async def test_get_ref_count_nonexistent(temp_backend: AsyncLocalBackend) -> None:
    """Test getting ref count of non-existent content."""
    fake_hash = "e" * 64

    result = await temp_backend.get_ref_count(fake_hash)
    assert not result.success


# === Streaming Tests ===


@pytest.mark.asyncio
async def test_stream_content_small_file(temp_backend: AsyncLocalBackend) -> None:
    """Test streaming a small file asynchronously."""
    content = b"Small file content for streaming test"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Stream content using async generator
    chunks = []
    async for chunk in temp_backend.stream_content(content_hash, chunk_size=10):
        chunks.append(chunk)

    # Verify chunks reassemble to original content
    streamed_content = b"".join(chunks)
    assert streamed_content == content

    # Verify streaming produced multiple chunks
    assert len(chunks) > 1


@pytest.mark.asyncio
async def test_stream_content_large_file(temp_backend: AsyncLocalBackend) -> None:
    """Test streaming a large file in chunks asynchronously."""
    # Create 1MB test file
    content = b"X" * (1024 * 1024)
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Stream in 64KB chunks
    chunk_size = 64 * 1024
    chunks = []
    async for chunk in temp_backend.stream_content(content_hash, chunk_size=chunk_size):
        chunks.append(chunk)

    # Verify chunks reassemble correctly
    streamed_content = b"".join(chunks)
    assert streamed_content == content

    # Verify chunk count (should be ~16 chunks for 1MB / 64KB)
    assert len(chunks) == 16


@pytest.mark.asyncio
async def test_stream_content_missing_file(temp_backend: AsyncLocalBackend) -> None:
    """Test that streaming non-existent content raises error."""
    fake_hash = "0" * 64

    with pytest.raises(NexusFileNotFoundError):
        async for _ in temp_backend.stream_content(fake_hash):
            pass


# === Write Stream Tests ===


@pytest.mark.asyncio
async def test_write_stream_basic(temp_backend: AsyncLocalBackend) -> None:
    """Test writing content from an async iterator."""
    chunks = [b"Hello, ", b"World", b"!"]
    expected_content = b"".join(chunks)

    async def chunk_generator():
        for chunk in chunks:
            yield chunk

    result = await temp_backend.write_stream(chunk_generator())
    content_hash = result.unwrap()

    # Verify hash matches expected content
    expected_hash = hash_content(expected_content)
    assert content_hash == expected_hash

    # Verify content can be read back
    read_result = await temp_backend.read_content(content_hash)
    assert read_result.unwrap() == expected_content


@pytest.mark.asyncio
async def test_write_stream_large_content(temp_backend: AsyncLocalBackend) -> None:
    """Test writing large content through stream."""
    chunk_size = 64 * 1024  # 64KB chunks
    total_size = 1024 * 1024  # 1MB total

    async def chunk_generator():
        remaining = total_size
        while remaining > 0:
            size = min(chunk_size, remaining)
            yield b"X" * size
            remaining -= size

    result = await temp_backend.write_stream(chunk_generator())
    content_hash = result.unwrap()

    # Verify size
    size_result = await temp_backend.get_content_size(content_hash)
    assert size_result.unwrap() == total_size


# === Batch Read Tests ===


@pytest.mark.asyncio
async def test_batch_read_content_basic(temp_backend: AsyncLocalBackend) -> None:
    """Test batch reading multiple content items asynchronously."""
    # Write multiple content items
    content1 = b"Content 1"
    content2 = b"Content 2"
    content3 = b"Content 3"

    hash1 = (await temp_backend.write_content(content1)).unwrap()
    hash2 = (await temp_backend.write_content(content2)).unwrap()
    hash3 = (await temp_backend.write_content(content3)).unwrap()

    # Batch read all content
    result = await temp_backend.batch_read_content([hash1, hash2, hash3])

    assert len(result) == 3
    assert result[hash1] == content1
    assert result[hash2] == content2
    assert result[hash3] == content3


@pytest.mark.asyncio
async def test_batch_read_content_missing_hashes(temp_backend: AsyncLocalBackend) -> None:
    """Test batch read with some missing content hashes."""
    # Write one content item
    content1 = b"Content 1"
    hash1 = (await temp_backend.write_content(content1)).unwrap()

    # Create fake hashes that don't exist
    fake_hash1 = "0" * 64
    fake_hash2 = "1" * 64

    # Batch read with mix of existing and missing
    result = await temp_backend.batch_read_content([hash1, fake_hash1, fake_hash2])

    assert len(result) == 3
    assert result[hash1] == content1
    assert result[fake_hash1] is None  # Missing content returns None
    assert result[fake_hash2] is None


@pytest.mark.asyncio
async def test_batch_read_content_empty_list(temp_backend: AsyncLocalBackend) -> None:
    """Test batch read with empty list."""
    result = await temp_backend.batch_read_content([])
    assert result == {}


@pytest.mark.asyncio
async def test_batch_read_content_concurrent(temp_backend: AsyncLocalBackend) -> None:
    """Test that batch read uses concurrent I/O."""
    # Write 20 files
    contents = [f"Content for concurrent test file {i}".encode() for i in range(20)]
    hashes = []
    for content in contents:
        result = await temp_backend.write_content(content)
        hashes.append(result.unwrap())

    # Batch read all files (should be concurrent)
    result = await temp_backend.batch_read_content(hashes)

    # Verify all content was read correctly
    assert len(result) == 20
    for i, h in enumerate(hashes):
        assert result[h] == contents[i]


# === Directory Operations ===


@pytest.mark.asyncio
async def test_mkdir(temp_backend: AsyncLocalBackend) -> None:
    """Test creating directories asynchronously."""
    dir_path = "/test/nested/directory"
    result = await temp_backend.mkdir(dir_path, parents=True)
    result.unwrap()

    # Check that directory was created
    physical_path = temp_backend.dir_root / dir_path.lstrip("/")
    assert physical_path.exists()
    assert physical_path.is_dir()


@pytest.mark.asyncio
async def test_mkdir_existing(temp_backend: AsyncLocalBackend) -> None:
    """Test creating directory that already exists with exist_ok=True."""
    dir_path = "/test/existing"
    result1 = await temp_backend.mkdir(dir_path, parents=True, exist_ok=True)
    result1.unwrap()

    # Create again - should not raise
    result2 = await temp_backend.mkdir(dir_path, exist_ok=True)
    result2.unwrap()


@pytest.mark.asyncio
async def test_is_directory(temp_backend: AsyncLocalBackend) -> None:
    """Test checking if path is a directory."""
    dir_path = "/test/directory"
    await temp_backend.mkdir(dir_path, parents=True)

    result = await temp_backend.is_directory(dir_path)
    assert result.unwrap() is True


@pytest.mark.asyncio
async def test_list_directory(temp_backend: AsyncLocalBackend) -> None:
    """Test listing directory contents asynchronously."""
    # Create a directory structure
    await temp_backend.mkdir("/test", parents=True, exist_ok=True)
    await temp_backend.mkdir("/test/sub1", exist_ok=True)
    await temp_backend.mkdir("/test/sub2", exist_ok=True)

    items = await temp_backend.list_dir("/test")

    # list_dir returns directories with trailing slashes
    assert "sub1/" in items
    assert "sub2/" in items


# === Concurrent Write Tests ===


@pytest.mark.asyncio
async def test_concurrent_writes_same_content(temp_backend: AsyncLocalBackend) -> None:
    """Test that concurrent writes of same content are handled correctly."""
    content = b"Concurrent write content"

    # Write same content concurrently
    tasks = [temp_backend.write_content(content) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All should return same hash
    hashes = [r.unwrap() for r in results]
    assert len(set(hashes)) == 1  # All same hash

    # Ref count should be 10
    ref_result = await temp_backend.get_ref_count(hashes[0])
    assert ref_result.unwrap() == 10

    # Content should be readable
    read_result = await temp_backend.read_content(hashes[0])
    assert read_result.unwrap() == content


@pytest.mark.asyncio
async def test_concurrent_writes_different_content(temp_backend: AsyncLocalBackend) -> None:
    """Test concurrent writes of different content."""
    contents = [f"Content {i}".encode() for i in range(20)]

    # Write all content concurrently
    tasks = [temp_backend.write_content(c) for c in contents]
    results = await asyncio.gather(*tasks)

    # All should succeed with unique hashes
    hashes = [r.unwrap() for r in results]
    assert len(set(hashes)) == 20  # All unique

    # All content should be readable
    for content, h in zip(contents, hashes):
        read_result = await temp_backend.read_content(h)
        assert read_result.unwrap() == content


@pytest.mark.asyncio
async def test_concurrent_read_write(temp_backend: AsyncLocalBackend) -> None:
    """Test concurrent reads and writes don't interfere."""
    content = b"Read-write test content"
    result = await temp_backend.write_content(content)
    content_hash = result.unwrap()

    # Perform concurrent reads and writes
    async def read_task():
        result = await temp_backend.read_content(content_hash)
        return result.unwrap()

    async def write_task():
        result = await temp_backend.write_content(content)
        return result.unwrap()

    tasks = [read_task() for _ in range(10)] + [write_task() for _ in range(5)]
    results = await asyncio.gather(*tasks)

    # All reads should return the correct content
    for result in results[:10]:
        assert result == content

    # All writes should return the same hash
    for result in results[10:]:
        assert result == content_hash


# === Cache Tests ===


@pytest.mark.asyncio
async def test_cache_hit_on_read(backend_with_cache: AsyncLocalBackend) -> None:
    """Test that cache is used on read."""
    content = b"Cached content"
    result = await backend_with_cache.write_content(content)
    content_hash = result.unwrap()

    # First read - should populate cache
    read_result = await backend_with_cache.read_content(content_hash)
    assert read_result.unwrap() == content

    # Verify cache was populated
    assert backend_with_cache.content_cache.get(content_hash) == content

    # Second read - should hit cache
    read_result = await backend_with_cache.read_content(content_hash)
    assert read_result.unwrap() == content


@pytest.mark.asyncio
async def test_batch_read_uses_cache(backend_with_cache: AsyncLocalBackend) -> None:
    """Test that batch read uses cache."""
    content1 = b"Cached content 1"
    content2 = b"Cached content 2"
    hash1 = (await backend_with_cache.write_content(content1)).unwrap()
    hash2 = (await backend_with_cache.write_content(content2)).unwrap()

    # First batch read (populates cache)
    result1 = await backend_with_cache.batch_read_content([hash1, hash2])
    assert result1[hash1] == content1
    assert result1[hash2] == content2

    # Verify cache was populated
    assert backend_with_cache.content_cache.get(hash1) == content1
    assert backend_with_cache.content_cache.get(hash2) == content2

    # Second batch read (should hit cache)
    result2 = await backend_with_cache.batch_read_content([hash1, hash2])
    assert result2[hash1] == content1
    assert result2[hash2] == content2


# === Hash/Path Utility Tests ===


@pytest.mark.asyncio
async def test_hash_to_path(temp_backend: AsyncLocalBackend) -> None:
    """Test hash to path conversion."""
    content_hash = "abcdef1234567890" + "0" * 48  # 64 char hash

    path = temp_backend._hash_to_path(content_hash)

    # Should create two-level directory structure
    assert path.parent.name == "cd"
    assert path.parent.parent.name == "ab"
    assert path.name == content_hash


@pytest.mark.asyncio
async def test_hash_to_path_invalid(temp_backend: AsyncLocalBackend) -> None:
    """Test hash to path with invalid hash length."""
    with pytest.raises(ValueError, match="Invalid hash length"):
        temp_backend._hash_to_path("abc")


# === Multiple Backend Instances ===


@pytest.mark.asyncio
async def test_multiple_backends_same_root(tmp_path: Path) -> None:
    """Test that multiple async backend instances can share same root."""
    root = tmp_path / "shared_backend"

    backend1 = AsyncLocalBackend(root)
    backend2 = AsyncLocalBackend(root)
    await backend1.initialize()
    await backend2.initialize()

    try:
        # Write with first backend
        content = b"Shared content"
        result = await backend1.write_content(content)
        hash1 = result.unwrap()

        # Read with second backend
        read_result = await backend2.read_content(hash1)
        retrieved = read_result.unwrap()
        assert retrieved == content
    finally:
        await backend1.close()
        await backend2.close()


# === Error Handling Tests ===


@pytest.mark.asyncio
async def test_backend_error_on_invalid_root(tmp_path: Path) -> None:
    """Test that backend raises error for invalid root path (file instead of dir)."""
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as f:
        file_path = f.name

    try:
        with pytest.raises(BackendError):
            backend = AsyncLocalBackend(file_path)
            await backend.initialize()
    finally:
        os.unlink(file_path)
