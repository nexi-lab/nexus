"""Integration tests for Embedded mode with CAS backend."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus.core.embedded import Embedded


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def embedded_cas(temp_dir: Path) -> Generator[Embedded, None, None]:
    """Create an Embedded instance (CAS always enabled)."""
    emb = Embedded(data_dir=temp_dir)
    yield emb
    emb.close()


def test_cas_write_and_read(embedded_cas: Embedded) -> None:
    """Test writing and reading with CAS."""
    content = b"Hello, CAS World!"

    embedded_cas.write("/test/file.txt", content)

    result = embedded_cas.read("/test/file.txt")
    assert result == content


def test_cas_automatic_deduplication(embedded_cas: Embedded) -> None:
    """Test that identical content is automatically deduplicated."""
    content = b"Duplicate content"

    # Write same content to two different paths
    embedded_cas.write("/file1.txt", content)
    embedded_cas.write("/file2.txt", content)

    # Check both files exist
    assert embedded_cas.exists("/file1.txt")
    assert embedded_cas.exists("/file2.txt")

    # Get metadata - should have same content hash (etag)
    meta1 = embedded_cas.metadata.get("/file1.txt")
    meta2 = embedded_cas.metadata.get("/file2.txt")

    assert meta1 is not None
    assert meta2 is not None
    assert meta1.etag == meta2.etag  # Same content hash
    assert meta1.physical_path == meta2.physical_path  # Same physical location

    # Verify ref count is 2 in CAS backend
    assert embedded_cas.backend.get_ref_count(meta1.etag) == 2


def test_cas_delete_with_ref_counting(embedded_cas: Embedded) -> None:
    """Test that delete properly handles reference counting."""
    content = b"Shared content"

    # Write same content to two paths
    embedded_cas.write("/shared1.txt", content)
    embedded_cas.write("/shared2.txt", content)

    meta1 = embedded_cas.metadata.get("/shared1.txt")
    content_hash = meta1.etag

    # Initial ref count should be 2
    assert embedded_cas.backend.get_ref_count(content_hash) == 2

    # Delete first file
    embedded_cas.delete("/shared1.txt")

    # First file should not exist
    assert not embedded_cas.exists("/shared1.txt")

    # Second file should still exist
    assert embedded_cas.exists("/shared2.txt")

    # Content should still exist in CAS with ref count 1
    assert embedded_cas.backend.content_exists(content_hash)
    assert embedded_cas.backend.get_ref_count(content_hash) == 1

    # Delete second file
    embedded_cas.delete("/shared2.txt")

    # Content should now be deleted from CAS
    assert not embedded_cas.backend.content_exists(content_hash)


def test_cas_update_file_content(embedded_cas: Embedded) -> None:
    """Test updating file content handles ref counting correctly."""
    content1 = b"Original content"
    content2 = b"Updated content"

    # Write initial content
    embedded_cas.write("/test.txt", content1)
    meta1 = embedded_cas.metadata.get("/test.txt")
    hash1 = meta1.etag

    # Verify ref count
    assert embedded_cas.backend.get_ref_count(hash1) == 1

    # Update with new content
    embedded_cas.write("/test.txt", content2)
    meta2 = embedded_cas.metadata.get("/test.txt")
    hash2 = meta2.etag

    # Hash should be different
    assert hash1 != hash2

    # Old content should be deleted
    assert not embedded_cas.backend.content_exists(hash1)

    # New content should exist
    assert embedded_cas.backend.content_exists(hash2)
    assert embedded_cas.backend.get_ref_count(hash2) == 1


def test_cas_storage_efficiency(embedded_cas: Embedded) -> None:
    """Test storage efficiency with multiple files."""
    content = b"x" * 1000  # 1KB

    # Write same content 10 times
    for i in range(10):
        embedded_cas.write(f"/file{i}.txt", content)

    # All files should exist
    for i in range(10):
        assert embedded_cas.exists(f"/file{i}.txt")

    # Get content hash
    meta = embedded_cas.metadata.get("/file0.txt")
    content_hash = meta.etag

    # Ref count should be 10
    assert embedded_cas.backend.get_ref_count(content_hash) == 10

    # Content should only be stored once
    assert embedded_cas.backend.get_content_size(content_hash) == 1000


def test_cas_different_content_different_hashes(embedded_cas: Embedded) -> None:
    """Test that different content produces different hashes."""
    embedded_cas.write("/file1.txt", b"Content A")
    embedded_cas.write("/file2.txt", b"Content B")

    meta1 = embedded_cas.metadata.get("/file1.txt")
    meta2 = embedded_cas.metadata.get("/file2.txt")

    # Different content should have different hashes
    assert meta1.etag != meta2.etag


def test_cas_list_files(embedded_cas: Embedded) -> None:
    """Test listing files with CAS."""
    embedded_cas.write("/dir1/file1.txt", b"Content 1")
    embedded_cas.write("/dir1/file2.txt", b"Content 2")
    embedded_cas.write("/dir2/file3.txt", b"Content 3")

    all_files = embedded_cas.list()
    assert len(all_files) == 3
    assert "/dir1/file1.txt" in all_files
    assert "/dir1/file2.txt" in all_files
    assert "/dir2/file3.txt" in all_files


def test_cas_binary_content(embedded_cas: Embedded) -> None:
    """Test CAS with binary content."""
    content = bytes(range(256))

    embedded_cas.write("/binary.bin", content)

    result = embedded_cas.read("/binary.bin")
    assert result == content


def test_cas_empty_content(embedded_cas: Embedded) -> None:
    """Test CAS with empty content."""
    embedded_cas.write("/empty.txt", b"")

    result = embedded_cas.read("/empty.txt")
    assert result == b""


def test_cas_large_content(embedded_cas: Embedded) -> None:
    """Test CAS with large content."""
    content = b"x" * (1024 * 1024)  # 1MB

    embedded_cas.write("/large.bin", content)

    result = embedded_cas.read("/large.bin")
    assert len(result) == len(content)
    assert result == content


def test_cas_metadata_stored_correctly(embedded_cas: Embedded) -> None:
    """Test that metadata is stored correctly with CAS."""
    content = b"Test metadata"

    embedded_cas.write("/test.txt", content)

    meta = embedded_cas.metadata.get("/test.txt")
    assert meta is not None
    assert meta.path == "/test.txt"
    assert meta.backend_name == "local"
    assert meta.size == len(content)
    assert meta.etag == meta.physical_path  # In CAS, etag = hash = physical path
    assert len(meta.etag) == 64  # SHA-256 hash length


def test_cas_concurrent_deduplication(embedded_cas: Embedded) -> None:
    """Test deduplication with multiple writes of same content."""
    content = b"Concurrent content"

    # Write same content multiple times rapidly
    paths = [f"/concurrent{i}.txt" for i in range(20)]
    for path in paths:
        embedded_cas.write(path, content)

    # All files should exist
    for path in paths:
        assert embedded_cas.exists(path)

    # Get content hash
    meta = embedded_cas.metadata.get(paths[0])
    content_hash = meta.etag

    # Ref count should be 20
    assert embedded_cas.backend.get_ref_count(content_hash) == 20

    # Delete all files
    for path in paths:
        embedded_cas.delete(path)

    # Content should be completely deleted
    assert not embedded_cas.backend.content_exists(content_hash)


def test_cas_update_preserves_timestamps(embedded_cas: Embedded) -> None:
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
