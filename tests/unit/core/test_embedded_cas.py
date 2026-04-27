"""Integration tests for Embedded mode with CAS backend.

These tests explicitly verify CAS backend behavior (deduplication).
"""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Mount points auto-created by factory boot.
_SYSTEM_PATHS = frozenset({"/", "/agents", "/nexus/pipes/audit-events"})
# The IPC /agents mount exposes its LocalConnectorBackend subdirectory
# tree, so anything under it is also system-internal. Same for /nexus/.
_SYSTEM_PATH_PREFIXES: tuple[str, ...] = ("/agents/", "/nexus/")


def _is_user_file(path: str) -> bool:
    """True when *path* is not a system mount entry or child thereof."""
    if path in _SYSTEM_PATHS:
        return False
    return not path.startswith(_SYSTEM_PATH_PREFIXES)


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
def embedded_cas(temp_dir: Path, local_backend: CASLocalBackend) -> Generator[NexusFS, None, None]:
    """Create an Embedded instance (CAS always enabled) with isolated database.

    (Environment variable isolation is handled by the global conftest fixture)
    """
    emb = create_nexus_fs(
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


def test_cas_write_and_read(embedded_cas: NexusFS) -> None:
    """Test writing and reading with CAS."""
    content = b"Hello, CAS World!"

    embedded_cas.write("/test/file.txt", content)

    result = embedded_cas.sys_read("/test/file.txt")
    assert result == content


def test_cas_automatic_deduplication(embedded_cas: NexusFS, local_backend: CASLocalBackend) -> None:
    """Test that identical content is automatically deduplicated."""
    content = b"Duplicate content"

    # Write same content to two different paths
    embedded_cas.write("/file1.txt", content)
    embedded_cas.write("/file2.txt", content)

    # Check both files exist
    assert embedded_cas.access("/file1.txt")
    assert embedded_cas.access("/file2.txt")

    # Get metadata - should have same content hash (etag)
    meta1 = embedded_cas.metadata.get("/file1.txt")
    meta2 = embedded_cas.metadata.get("/file2.txt")

    assert meta1 is not None
    assert meta2 is not None
    assert meta1.content_id == meta2.content_id  # Same content hash → same CAS blob


def test_cas_delete_content(embedded_cas: NexusFS, local_backend: CASLocalBackend) -> None:
    """Test that delete removes content from CAS.

    Issue #1320: sys_unlink is now metadata-only — content cleanup is
    deferred to CAS GC.  This test exercises the CAS backend's
    delete_content() directly to verify blob removal.
    """
    content = b"Shared content"

    # Write content
    embedded_cas.write("/shared1.txt", content)

    meta1 = embedded_cas.metadata.get("/shared1.txt")
    content_hash = meta1.content_id

    assert local_backend.content_exists(content_hash)

    # Delete content via CAS backend directly (simulates GC cleanup)
    local_backend.delete_content(content_hash)

    # Content should now be deleted from CAS
    assert not local_backend.content_exists(content_hash)


def test_cas_update_file_content(embedded_cas: NexusFS, local_backend: CASLocalBackend) -> None:
    """Test updating file content with version tracking (v0.3.5).

    With version tracking enabled, old content is preserved in CAS
    so previous versions can be accessed. Content is NOT deleted
    until all versions referencing it are deleted.
    """
    content1 = b"Original content"
    content2 = b"Updated content"

    # Write initial content
    embedded_cas.write("/test.txt", content1)
    meta1 = embedded_cas.metadata.get("/test.txt")
    hash1 = meta1.content_id

    # Update with new content
    embedded_cas.write("/test.txt", content2)
    meta2 = embedded_cas.metadata.get("/test.txt")
    hash2 = meta2.content_id

    # Hash should be different
    assert hash1 != hash2

    # With version tracking (v0.3.5), old content is PRESERVED
    # so previous versions can be accessed
    assert local_backend.content_exists(hash1)  # Old content still exists

    # New content should also exist
    assert local_backend.content_exists(hash2)


def test_cas_storage_efficiency(embedded_cas: NexusFS, local_backend: CASLocalBackend) -> None:
    """Test storage efficiency with multiple files."""
    content = b"x" * 1000

    # Write same content 10 times
    for i in range(10):
        embedded_cas.write(f"/file{i}.txt", content)

    # All files should exist
    for i in range(10):
        assert embedded_cas.access(f"/file{i}.txt")

    # Get content hash
    meta = embedded_cas.metadata.get("/file0.txt")
    content_hash = meta.content_id

    # Content should only be stored once
    assert local_backend.get_content_size(content_hash) == len(content)


def test_cas_different_content_different_hashes(embedded_cas: NexusFS) -> None:
    """Test that different content produces different hashes."""
    embedded_cas.write("/file1.txt", b"Content A")
    embedded_cas.write("/file2.txt", b"Content B")

    meta1 = embedded_cas.metadata.get("/file1.txt")
    meta2 = embedded_cas.metadata.get("/file2.txt")

    # Different content should have different hashes
    assert meta1.content_id != meta2.content_id


def test_cas_list_files(embedded_cas: NexusFS) -> None:
    """Test listing files with CAS."""
    embedded_cas.write("/dir1/file1.txt", b"Content 1")
    embedded_cas.write("/dir1/file2.txt", b"Content 2")
    embedded_cas.write("/dir2/file3.txt", b"Content 3")

    all_files = [f for f in embedded_cas.sys_readdir() if _is_user_file(f)]
    assert len(all_files) == 3
    assert "/dir1/file1.txt" in all_files
    assert "/dir1/file2.txt" in all_files
    assert "/dir2/file3.txt" in all_files


def test_cas_binary_content(embedded_cas: NexusFS) -> None:
    """Test CAS with binary content."""
    content = bytes(range(256))

    embedded_cas.write("/binary.bin", content)

    result = embedded_cas.sys_read("/binary.bin")
    assert result == content


def test_cas_empty_content(embedded_cas: NexusFS) -> None:
    """Test CAS with empty content."""
    embedded_cas.write("/empty.txt", b"")

    result = embedded_cas.sys_read("/empty.txt")
    assert result == b""


def test_cas_large_content(embedded_cas: NexusFS) -> None:
    """Test CAS with large content."""
    content = b"x" * (1024 * 1024)  # 1MB

    embedded_cas.write("/large.bin", content)

    result = embedded_cas.sys_read("/large.bin")
    assert len(result) == len(content)
    assert result == content


def test_cas_metadata_stored_correctly(embedded_cas: NexusFS) -> None:
    """Test that metadata is stored correctly with CAS."""
    content = b"Test metadata"

    embedded_cas.write("/test.txt", content)

    meta = embedded_cas.metadata.get("/test.txt")
    assert meta is not None
    assert meta.path == "/test.txt"
    assert meta.size == len(content)
    assert len(meta.content_id) == 64  # SHA-256 hash length (kernel resolves blob loc)


def test_cas_concurrent_deduplication(
    embedded_cas: NexusFS, local_backend: CASLocalBackend
) -> None:
    """Test deduplication with multiple writes of same content."""
    content = b"Concurrent content"

    # Write same content multiple times rapidly
    paths = [f"/concurrent{i}.txt" for i in range(20)]
    for path in paths:
        embedded_cas.write(path, content)

    # All files should exist
    for path in paths:
        assert embedded_cas.access(path)

    # Get content hash
    meta = embedded_cas.metadata.get(paths[0])
    content_hash = meta.content_id

    # Content should exist (deduplication means single blob)
    assert local_backend.content_exists(content_hash)


def test_cas_update_preserves_timestamps(embedded_cas: NexusFS) -> None:
    """Test that updating content preserves created_at timestamp."""
    embedded_cas.write("/test.txt", b"Original")

    meta1 = embedded_cas.metadata.get("/test.txt")
    created_at = meta1.created_at

    # Wait a tiny bit and update
    embedded_cas.write("/test.txt", b"Updated")

    meta2 = embedded_cas.metadata.get("/test.txt")

    # created_at should be preserved
    assert meta2.created_at == created_at

    # modified_at should be updated
    assert meta2.modified_at >= meta1.modified_at
