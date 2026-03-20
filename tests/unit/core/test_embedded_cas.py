"""Integration tests for Embedded mode with CAS backend.

These tests explicitly verify CAS backend behavior (ref counting,
deduplication).
"""

import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Mount points auto-created by factory boot.
_SYSTEM_PATHS = frozenset({"/", "/agents", "/nexus/pipes/audit-events"})


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def local_backend(temp_dir: Path) -> CASLocalBackend:
    """Create a CASLocalBackend for direct CAS operations in tests."""
    return CASLocalBackend(temp_dir)


@pytest.fixture
async def embedded_cas(
    temp_dir: Path, local_backend: CASLocalBackend
) -> AsyncGenerator[NexusFS, None]:
    """Create an Embedded instance (CAS always enabled) with isolated database.

    (Environment variable isolation is handled by the global conftest fixture)
    """
    emb = await create_nexus_fs(
        backend=local_backend,
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        parsing=ParseConfig(auto_parse=False),  # Disable auto-parsing for unit tests
        permissions=PermissionConfig(
            enforce=False
        ),  # Disable permissions for basic functionality tests
    )
    yield emb
    emb.close()

    # On Windows, give OS extra time to release file handles after close
    # This is necessary because SQLite WAL files may still be locked
    import gc
    import platform
    import time

    gc.collect()  # Force garbage collection to release connections
    if platform.system() == "Windows":
        time.sleep(0.05)  # 50ms extra delay on Windows


@pytest.mark.asyncio
async def test_cas_write_and_read(embedded_cas: NexusFS) -> None:
    """Test writing and reading with CAS."""
    content = b"Hello, CAS World!"

    await embedded_cas.sys_write("/test/file.txt", content)

    result = await embedded_cas.sys_read("/test/file.txt")
    assert result == content


@pytest.mark.asyncio
async def test_cas_automatic_deduplication(
    embedded_cas: NexusFS, local_backend: CASLocalBackend
) -> None:
    """Test that identical content is automatically deduplicated."""
    content = b"Duplicate content"

    # Write same content to two different paths
    await embedded_cas.sys_write("/file1.txt", content)
    await embedded_cas.sys_write("/file2.txt", content)

    # Check both files exist
    assert await embedded_cas.sys_access("/file1.txt")
    assert await embedded_cas.sys_access("/file2.txt")

    # Get metadata - should have same content hash (etag)
    meta1 = embedded_cas.metadata.get("/file1.txt")
    meta2 = embedded_cas.metadata.get("/file2.txt")

    assert meta1 is not None
    assert meta2 is not None
    assert meta1.etag == meta2.etag  # Same content hash
    assert meta1.physical_path == meta2.physical_path  # Same physical location

    # Verify ref count is 2 in CAS backend
    assert local_backend.get_ref_count(meta1.etag) == 2


@pytest.mark.asyncio
async def test_cas_delete_with_ref_counting(
    embedded_cas: NexusFS, local_backend: CASLocalBackend
) -> None:
    """Test that delete properly handles reference counting."""
    content = b"Shared content"

    # Write same content to two paths
    await embedded_cas.sys_write("/shared1.txt", content)
    await embedded_cas.sys_write("/shared2.txt", content)

    meta1 = embedded_cas.metadata.get("/shared1.txt")
    content_hash = meta1.etag

    # Initial ref count should be 2
    assert local_backend.get_ref_count(content_hash) == 2

    # Delete first file
    await embedded_cas.sys_unlink("/shared1.txt")

    # First file should not exist
    assert not await embedded_cas.sys_access("/shared1.txt")

    # Second file should still exist
    assert await embedded_cas.sys_access("/shared2.txt")

    # Content should still exist in CAS with ref count 1
    assert local_backend.content_exists(content_hash)
    assert local_backend.get_ref_count(content_hash) == 1

    # Delete second file
    await embedded_cas.sys_unlink("/shared2.txt")

    # Content should now be deleted from CAS
    assert not local_backend.content_exists(content_hash)


@pytest.mark.asyncio
async def test_cas_update_file_content(
    embedded_cas: NexusFS, local_backend: CASLocalBackend
) -> None:
    """Test updating file content with version tracking (v0.3.5).

    With version tracking enabled, old content is preserved in CAS
    so previous versions can be accessed. Content is NOT deleted
    until all versions referencing it are deleted.
    """
    content1 = b"Original content"
    content2 = b"Updated content"

    # Write initial content
    await embedded_cas.sys_write("/test.txt", content1)
    meta1 = embedded_cas.metadata.get("/test.txt")
    hash1 = meta1.etag

    # Verify ref count
    assert local_backend.get_ref_count(hash1) == 1

    # Update with new content
    await embedded_cas.sys_write("/test.txt", content2)
    meta2 = embedded_cas.metadata.get("/test.txt")
    hash2 = meta2.etag

    # Hash should be different
    assert hash1 != hash2

    # With version tracking (v0.3.5), old content is PRESERVED
    # so previous versions can be accessed
    assert local_backend.content_exists(hash1)  # Old content still exists

    # New content should also exist
    assert local_backend.content_exists(hash2)
    assert local_backend.get_ref_count(hash2) == 1


@pytest.mark.asyncio
async def test_cas_storage_efficiency(
    embedded_cas: NexusFS, local_backend: CASLocalBackend
) -> None:
    """Test storage efficiency with multiple files."""
    content = b"x" * 1000

    # Write same content 10 times
    for i in range(10):
        await embedded_cas.sys_write(f"/file{i}.txt", content)

    # All files should exist
    for i in range(10):
        assert await embedded_cas.sys_access(f"/file{i}.txt")

    # Get content hash
    meta = embedded_cas.metadata.get("/file0.txt")
    content_hash = meta.etag

    # Ref count should be 10
    assert local_backend.get_ref_count(content_hash) == 10

    # Content should only be stored once
    assert local_backend.get_content_size(content_hash) == len(content)


@pytest.mark.asyncio
async def test_cas_different_content_different_hashes(embedded_cas: NexusFS) -> None:
    """Test that different content produces different hashes."""
    await embedded_cas.sys_write("/file1.txt", b"Content A")
    await embedded_cas.sys_write("/file2.txt", b"Content B")

    meta1 = embedded_cas.metadata.get("/file1.txt")
    meta2 = embedded_cas.metadata.get("/file2.txt")

    # Different content should have different hashes
    assert meta1.etag != meta2.etag


@pytest.mark.asyncio
async def test_cas_list_files(embedded_cas: NexusFS) -> None:
    """Test listing files with CAS."""
    await embedded_cas.sys_write("/dir1/file1.txt", b"Content 1")
    await embedded_cas.sys_write("/dir1/file2.txt", b"Content 2")
    await embedded_cas.sys_write("/dir2/file3.txt", b"Content 3")

    all_files = [f for f in await embedded_cas.sys_readdir() if f not in _SYSTEM_PATHS]
    assert len(all_files) == 3
    assert "/dir1/file1.txt" in all_files
    assert "/dir1/file2.txt" in all_files
    assert "/dir2/file3.txt" in all_files


@pytest.mark.asyncio
async def test_cas_binary_content(embedded_cas: NexusFS) -> None:
    """Test CAS with binary content."""
    content = bytes(range(256))

    await embedded_cas.sys_write("/binary.bin", content)

    result = await embedded_cas.sys_read("/binary.bin")
    assert result == content


@pytest.mark.asyncio
async def test_cas_empty_content(embedded_cas: NexusFS) -> None:
    """Test CAS with empty content."""
    await embedded_cas.sys_write("/empty.txt", b"")

    result = await embedded_cas.sys_read("/empty.txt")
    assert result == b""


@pytest.mark.asyncio
async def test_cas_large_content(embedded_cas: NexusFS) -> None:
    """Test CAS with large content."""
    content = b"x" * (1024 * 1024)  # 1MB

    await embedded_cas.sys_write("/large.bin", content)

    result = await embedded_cas.sys_read("/large.bin")
    assert len(result) == len(content)
    assert result == content


@pytest.mark.asyncio
async def test_cas_metadata_stored_correctly(embedded_cas: NexusFS) -> None:
    """Test that metadata is stored correctly with CAS."""
    content = b"Test metadata"

    await embedded_cas.sys_write("/test.txt", content)

    meta = embedded_cas.metadata.get("/test.txt")
    assert meta is not None
    assert meta.path == "/test.txt"
    assert meta.backend_name == "local"
    assert meta.size == len(content)
    assert meta.etag == meta.physical_path  # In CAS, etag = hash = physical path
    assert len(meta.etag) == 64  # SHA-256 hash length


@pytest.mark.asyncio
async def test_cas_concurrent_deduplication(
    embedded_cas: NexusFS, local_backend: CASLocalBackend
) -> None:
    """Test deduplication with multiple writes of same content."""
    content = b"Concurrent content"

    # Write same content multiple times rapidly
    paths = [f"/concurrent{i}.txt" for i in range(20)]
    for path in paths:
        await embedded_cas.sys_write(path, content)

    # All files should exist
    for path in paths:
        assert await embedded_cas.sys_access(path)

    # Get content hash
    meta = embedded_cas.metadata.get(paths[0])
    content_hash = meta.etag

    # Ref count should be 20
    assert local_backend.get_ref_count(content_hash) == 20

    # Delete all files
    for path in paths:
        await embedded_cas.sys_unlink(path)

    # Content should be completely deleted
    assert not local_backend.content_exists(content_hash)


@pytest.mark.asyncio
async def test_cas_update_preserves_timestamps(embedded_cas: NexusFS) -> None:
    """Test that updating content preserves created_at timestamp."""
    await embedded_cas.sys_write("/test.txt", b"Original")

    meta1 = embedded_cas.metadata.get("/test.txt")
    created_at = meta1.created_at

    # Wait a tiny bit and update
    await embedded_cas.sys_write("/test.txt", b"Updated")

    meta2 = embedded_cas.metadata.get("/test.txt")

    # created_at should be preserved
    assert meta2.created_at == created_at

    # modified_at should be updated
    assert meta2.modified_at >= meta1.modified_at
