"""Unit tests for AsyncNexusFS (Phase 3).

Tests async file operations that combine AsyncMetadataStore and AsyncLocalBackend.
Follows TDD approach: write failing tests first, then implement.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio

from nexus.core.async_nexus_fs import AsyncNexusFS
from nexus.core.exceptions import ConflictError, NexusFileNotFoundError
from nexus.storage.raft_metadata_store import RaftMetadataStore

pytestmark = [
    pytest.mark.xdist_group("async_nexus_fs"),
]


# === Fixtures ===


@pytest.fixture
def metadata_store(tmp_path: Path) -> RaftMetadataStore:
    """Create a local RaftMetadataStore backed by sled."""
    store = RaftMetadataStore.local(str(tmp_path / "raft"))
    yield store
    store.close()


@pytest_asyncio.fixture
async def async_fs(
    tmp_path: Path, metadata_store: RaftMetadataStore
) -> AsyncGenerator[AsyncNexusFS, None]:
    """Create AsyncNexusFS instance for testing."""
    fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        metadata_store=metadata_store,
        tenant_id="test-tenant",
    )
    await fs.initialize()
    yield fs
    await fs.close()


# === Initialization Tests ===


@pytest.mark.asyncio
async def test_initialization(tmp_path: Path, metadata_store: RaftMetadataStore) -> None:
    """Test AsyncNexusFS initialization."""
    fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        metadata_store=metadata_store,
        tenant_id="test-tenant",
    )
    await fs.initialize()

    assert fs.tenant_id == "test-tenant"
    assert fs.backend is not None
    assert fs.metadata is not None

    await fs.close()


# === Basic Read/Write Tests ===


@pytest.mark.asyncio
async def test_write_and_read(async_fs: AsyncNexusFS) -> None:
    """Test basic write and read operations."""
    path = "/test/hello.txt"
    content = b"Hello, World!"

    # Write file
    result = await async_fs.write(path, content)

    assert "etag" in result
    assert "version" in result
    assert result["size"] == len(content)

    # Read file back
    read_content = await async_fs.read(path)
    assert read_content == content


@pytest.mark.asyncio
async def test_write_string_content(async_fs: AsyncNexusFS) -> None:
    """Test writing string content (auto-converts to bytes)."""
    path = "/test/string.txt"
    content = "Hello, string world!"

    await async_fs.write(path, content)

    read_content = await async_fs.read(path)
    assert read_content == content.encode("utf-8")


@pytest.mark.asyncio
async def test_write_creates_parent_directories(async_fs: AsyncNexusFS) -> None:
    """Test that write creates parent directories automatically."""
    path = "/deeply/nested/path/file.txt"
    content = b"Nested content"

    await async_fs.write(path, content)

    # File should be readable
    read_content = await async_fs.read(path)
    assert read_content == content

    # Parent directories should exist
    assert await async_fs.exists("/deeply")
    assert await async_fs.exists("/deeply/nested")
    assert await async_fs.exists("/deeply/nested/path")


@pytest.mark.asyncio
async def test_write_overwrites_existing(async_fs: AsyncNexusFS) -> None:
    """Test that write overwrites existing content."""
    path = "/test/overwrite.txt"
    content1 = b"Original content"
    content2 = b"New content"

    # Write initial content
    result1 = await async_fs.write(path, content1)
    assert result1["version"] == 1

    # Overwrite
    result2 = await async_fs.write(path, content2)
    assert result2["version"] == 2
    assert result2["etag"] != result1["etag"]

    # Read should return new content
    read_content = await async_fs.read(path)
    assert read_content == content2


@pytest.mark.asyncio
async def test_read_nonexistent_file(async_fs: AsyncNexusFS) -> None:
    """Test reading non-existent file raises error."""
    with pytest.raises(NexusFileNotFoundError):
        await async_fs.read("/does/not/exist.txt")


@pytest.mark.asyncio
async def test_read_with_metadata(async_fs: AsyncNexusFS) -> None:
    """Test reading file with metadata."""
    path = "/test/with_meta.txt"
    content = b"Content with metadata"

    await async_fs.write(path, content)

    result = await async_fs.read(path, return_metadata=True)

    assert isinstance(result, dict)
    assert result["content"] == content
    assert "etag" in result
    assert "version" in result
    assert "modified_at" in result
    assert result["size"] == len(content)


# === Optimistic Concurrency Control Tests ===


@pytest.mark.asyncio
async def test_write_with_if_match_success(async_fs: AsyncNexusFS) -> None:
    """Test optimistic concurrency control with matching etag."""
    path = "/test/occ.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial content
    result1 = await async_fs.write(path, content1)
    etag = result1["etag"]

    # Write with matching etag - should succeed
    result2 = await async_fs.write(path, content2, if_match=etag)
    assert result2["version"] == 2


@pytest.mark.asyncio
async def test_write_with_if_match_conflict(async_fs: AsyncNexusFS) -> None:
    """Test optimistic concurrency control with mismatched etag."""
    path = "/test/occ_conflict.txt"
    content = b"Original content"

    await async_fs.write(path, content)

    # Try to write with wrong etag
    with pytest.raises(ConflictError):
        await async_fs.write(path, b"New content", if_match="wrong-etag")


@pytest.mark.asyncio
async def test_write_with_if_none_match(async_fs: AsyncNexusFS) -> None:
    """Test create-only mode with if_none_match."""
    path = "/test/create_only.txt"
    content = b"New file content"

    # First write should succeed (file doesn't exist)
    result = await async_fs.write(path, content, if_none_match=True)
    assert result["version"] == 1

    # Second write should fail (file exists)
    with pytest.raises(FileExistsError):
        await async_fs.write(path, b"Should fail", if_none_match=True)


# === Delete Tests ===


@pytest.mark.asyncio
async def test_delete(async_fs: AsyncNexusFS) -> None:
    """Test deleting a file."""
    path = "/test/to_delete.txt"
    content = b"Delete me"

    await async_fs.write(path, content)

    # Verify file exists
    assert await async_fs.exists(path)

    # Delete file
    result = await async_fs.delete(path)
    assert result["deleted"] is True

    # Verify file is deleted
    assert not await async_fs.exists(path)

    # Reading should fail
    with pytest.raises(NexusFileNotFoundError):
        await async_fs.read(path)


@pytest.mark.asyncio
async def test_delete_nonexistent(async_fs: AsyncNexusFS) -> None:
    """Test deleting non-existent file raises error."""
    with pytest.raises(NexusFileNotFoundError):
        await async_fs.delete("/does/not/exist.txt")


# === Exists Tests ===


@pytest.mark.asyncio
async def test_exists(async_fs: AsyncNexusFS) -> None:
    """Test checking file existence."""
    path = "/test/exists.txt"

    # File doesn't exist yet
    assert not await async_fs.exists(path)

    # Write file
    await async_fs.write(path, b"Content")

    # Now it exists
    assert await async_fs.exists(path)

    # Delete it
    await async_fs.delete(path)

    # No longer exists
    assert not await async_fs.exists(path)


# === Directory Operations ===


@pytest.mark.asyncio
async def test_mkdir(async_fs: AsyncNexusFS) -> None:
    """Test creating directories."""
    path = "/test/new_dir"

    # Need parents=True since /test doesn't exist
    await async_fs.mkdir(path, parents=True)

    assert await async_fs.exists(path)


@pytest.mark.asyncio
async def test_mkdir_parents(async_fs: AsyncNexusFS) -> None:
    """Test creating nested directories with parents=True."""
    path = "/test/nested/deep/directory"

    await async_fs.mkdir(path, parents=True)

    assert await async_fs.exists(path)
    assert await async_fs.exists("/test/nested/deep")
    assert await async_fs.exists("/test/nested")


@pytest.mark.asyncio
async def test_list_dir(async_fs: AsyncNexusFS) -> None:
    """Test listing directory contents."""
    # Create some files and directories
    await async_fs.write("/list_test/file1.txt", b"Content 1")
    await async_fs.write("/list_test/file2.txt", b"Content 2")
    await async_fs.mkdir("/list_test/subdir", parents=True)

    items = await async_fs.list_dir("/list_test")

    # Should contain files and subdirectory
    assert "file1.txt" in items
    assert "file2.txt" in items
    assert "subdir/" in items or "subdir" in items


@pytest.mark.asyncio
async def test_list_dir_empty(async_fs: AsyncNexusFS) -> None:
    """Test listing empty directory."""
    await async_fs.mkdir("/empty_dir", parents=True)

    items = await async_fs.list_dir("/empty_dir")
    assert items == []


# === Concurrent Operations ===


@pytest.mark.asyncio
async def test_concurrent_writes_different_files(async_fs: AsyncNexusFS) -> None:
    """Test concurrent writes to different files."""

    async def write_file(i: int) -> dict:
        path = f"/concurrent/file_{i}.txt"
        content = f"Content for file {i}".encode()
        return await async_fs.write(path, content)

    # Write 10 files concurrently
    tasks = [write_file(i) for i in range(10)]
    results = await asyncio.gather(*tasks)

    # All should succeed
    assert len(results) == 10

    # All files should be readable
    for i in range(10):
        path = f"/concurrent/file_{i}.txt"
        content = await async_fs.read(path)
        assert content == f"Content for file {i}".encode()


@pytest.mark.asyncio
async def test_concurrent_read_write(async_fs: AsyncNexusFS) -> None:
    """Test concurrent reads and writes don't interfere."""
    path = "/concurrent/rw.txt"
    content = b"Initial content"

    await async_fs.write(path, content)

    async def read_task() -> bytes:
        return await async_fs.read(path)

    async def write_task(i: int) -> dict:
        return await async_fs.write(f"/concurrent/other_{i}.txt", f"Other {i}".encode())

    # Mix reads and writes
    tasks = [read_task() for _ in range(5)] + [write_task(i) for i in range(5)]
    results = await asyncio.gather(*tasks)

    # All reads should return the content
    for result in results[:5]:
        assert result == content


# === Content Deduplication Tests ===


@pytest.mark.asyncio
async def test_content_deduplication(async_fs: AsyncNexusFS) -> None:
    """Test that identical content is deduplicated."""
    content = b"Duplicate content for dedup test"

    # Write same content to multiple files
    result1 = await async_fs.write("/dedup/file1.txt", content)
    result2 = await async_fs.write("/dedup/file2.txt", content)
    result3 = await async_fs.write("/dedup/file3.txt", content)

    # All should have the same etag (content hash)
    assert result1["etag"] == result2["etag"] == result3["etag"]

    # All files should be readable
    assert await async_fs.read("/dedup/file1.txt") == content
    assert await async_fs.read("/dedup/file2.txt") == content
    assert await async_fs.read("/dedup/file3.txt") == content


@pytest.mark.asyncio
async def test_delete_with_deduplication(async_fs: AsyncNexusFS) -> None:
    """Test that deleting one file doesn't affect others with same content."""
    content = b"Shared content"

    await async_fs.write("/shared/file1.txt", content)
    await async_fs.write("/shared/file2.txt", content)

    # Delete first file
    await async_fs.delete("/shared/file1.txt")

    # Second file should still work
    assert await async_fs.read("/shared/file2.txt") == content


# === Large File Tests ===


@pytest.mark.asyncio
async def test_write_large_content(async_fs: AsyncNexusFS) -> None:
    """Test writing large content."""
    # 5MB file
    content = b"X" * (5 * 1024 * 1024)

    result = await async_fs.write("/large/big_file.bin", content)
    assert result["size"] == len(content)

    # Read it back
    read_content = await async_fs.read("/large/big_file.bin")
    assert read_content == content


# === Empty Content Tests ===


@pytest.mark.asyncio
async def test_write_empty_content(async_fs: AsyncNexusFS) -> None:
    """Test writing empty content."""
    path = "/test/empty.txt"
    content = b""

    result = await async_fs.write(path, content)
    assert result["size"] == 0

    read_content = await async_fs.read(path)
    assert read_content == b""


# === Binary Content Tests ===


@pytest.mark.asyncio
async def test_binary_content(async_fs: AsyncNexusFS) -> None:
    """Test handling binary content with all byte values."""
    path = "/test/binary.bin"
    content = bytes(range(256))

    await async_fs.write(path, content)

    read_content = await async_fs.read(path)
    assert read_content == content


# === Streaming Tests ===


@pytest.mark.asyncio
async def test_stream_read(async_fs: AsyncNexusFS) -> None:
    """Test streaming read for large files."""
    path = "/stream/large.bin"
    content = b"X" * (1024 * 1024)  # 1MB

    await async_fs.write(path, content)

    # Stream read
    chunks = []
    async for chunk in async_fs.stream_read(path, chunk_size=64 * 1024):
        chunks.append(chunk)

    # Verify reassembled content
    assert b"".join(chunks) == content
    assert len(chunks) == 16  # 1MB / 64KB = 16 chunks


@pytest.mark.asyncio
async def test_stream_write(async_fs: AsyncNexusFS) -> None:
    """Test streaming write from async iterator."""
    path = "/stream/written.bin"
    chunks = [b"Chunk1", b"Chunk2", b"Chunk3"]

    async def chunk_generator():
        for chunk in chunks:
            yield chunk

    await async_fs.stream_write(path, chunk_generator())

    # Verify content
    expected_content = b"".join(chunks)
    read_content = await async_fs.read(path)
    assert read_content == expected_content


# === Metadata Query Tests ===


@pytest.mark.asyncio
async def test_get_metadata(async_fs: AsyncNexusFS) -> None:
    """Test getting file metadata."""
    path = "/test/meta.txt"
    content = b"Content for metadata"

    await async_fs.write(path, content)

    meta = await async_fs.get_metadata(path)

    assert meta is not None
    assert meta.size == len(content)
    assert meta.etag is not None
    assert meta.version == 1


@pytest.mark.asyncio
async def test_get_metadata_nonexistent(async_fs: AsyncNexusFS) -> None:
    """Test getting metadata for non-existent file."""
    meta = await async_fs.get_metadata("/does/not/exist.txt")
    assert meta is None


# === Batch Operations ===


@pytest.mark.asyncio
async def test_batch_read(async_fs: AsyncNexusFS) -> None:
    """Test reading multiple files in batch."""
    # Write some files
    paths_content = {
        "/batch/file1.txt": b"Content 1",
        "/batch/file2.txt": b"Content 2",
        "/batch/file3.txt": b"Content 3",
    }

    for path, content in paths_content.items():
        await async_fs.write(path, content)

    # Batch read
    results = await async_fs.batch_read(list(paths_content.keys()))

    # Verify all content
    for path, content in paths_content.items():
        assert results[path] == content


@pytest.mark.asyncio
async def test_batch_read_with_missing(async_fs: AsyncNexusFS) -> None:
    """Test batch read with some missing files."""
    await async_fs.write("/batch/exists.txt", b"Content")

    results = await async_fs.batch_read(
        [
            "/batch/exists.txt",
            "/batch/missing.txt",
        ]
    )

    assert results["/batch/exists.txt"] == b"Content"
    assert results["/batch/missing.txt"] is None


# === Path Validation Tests ===


@pytest.mark.asyncio
async def test_path_must_be_absolute(async_fs: AsyncNexusFS) -> None:
    """Test that paths must be absolute."""
    from nexus.core.exceptions import InvalidPathError

    with pytest.raises(InvalidPathError):
        await async_fs.write("relative/path.txt", b"Content")


@pytest.mark.asyncio
async def test_path_normalization(async_fs: AsyncNexusFS) -> None:
    """Test that paths are normalized."""
    content = b"Normalized content"

    # Write with extra slashes
    await async_fs.write("//test///normalized.txt", content)

    # Read with clean path
    read_content = await async_fs.read("/test/normalized.txt")
    assert read_content == content
