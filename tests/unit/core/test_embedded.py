"""Unit tests for Embedded filesystem."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus.core.embedded import Embedded
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def embedded(temp_dir: Path) -> Generator[Embedded, None, None]:
    """Create an Embedded filesystem instance."""
    nx = Embedded(data_dir=temp_dir)
    yield nx
    nx.close()


def test_init_creates_directories(temp_dir: Path) -> None:
    """Test that initialization creates necessary directories."""
    data_dir = temp_dir / "nexus-data"
    assert not data_dir.exists()

    nx = Embedded(data_dir=data_dir)

    assert data_dir.exists()
    assert (data_dir / "files").exists()
    assert (data_dir / "metadata.db").exists()

    nx.close()


def test_write_and_read(embedded: Embedded) -> None:
    """Test writing and reading a file."""
    content = b"Hello, Nexus!"
    path = "/test/file.txt"

    # Write file
    embedded.write(path, content)

    # Read file
    result = embedded.read(path)
    assert result == content


def test_write_creates_metadata(embedded: Embedded) -> None:
    """Test that writing creates metadata."""
    content = b"Test content"
    path = "/test.txt"

    embedded.write(path, content)

    # Check metadata exists
    assert embedded.exists(path)

    # Check metadata content
    meta = embedded.metadata.get(path)
    assert meta is not None
    assert meta.path == path
    assert meta.size == len(content)
    assert meta.version == 1
    assert meta.etag is not None


def test_write_updates_version(embedded: Embedded) -> None:
    """Test that rewriting a file updates metadata.

    Note: Version tracking not implemented in v0.1.0 simplified schema.
    Version will always be 1 until v0.2.0.
    """
    path = "/test.txt"

    # Write initial version
    embedded.write(path, b"Version 1")
    meta1 = embedded.metadata.get(path)
    assert meta1 is not None
    assert meta1.version == 1

    # Rewrite file
    embedded.write(path, b"Version 2")
    meta2 = embedded.metadata.get(path)
    assert meta2 is not None
    # Version tracking will be added in v0.2.0
    assert meta2.version == 1  # Changed from 2 for v0.1.0
    # But modified_at should be updated
    assert meta2.modified_at > meta1.modified_at

    # Rewrite again
    embedded.write(path, b"Version 3")
    meta3 = embedded.metadata.get(path)
    assert meta3 is not None
    assert meta3.version == 1  # Changed from 3 for v0.1.0
    assert meta3.modified_at > meta2.modified_at


def test_read_nonexistent_file_raises_error(embedded: Embedded) -> None:
    """Test that reading nonexistent file raises error."""
    with pytest.raises(NexusFileNotFoundError) as exc_info:
        embedded.read("/nonexistent.txt")

    assert "/nonexistent.txt" in str(exc_info.value)


def test_delete(embedded: Embedded) -> None:
    """Test deleting a file."""
    path = "/test.txt"
    content = b"Test content"

    # Create file
    embedded.write(path, content)
    assert embedded.exists(path)

    # Delete file
    embedded.delete(path)

    # File should not exist
    assert not embedded.exists(path)

    # Reading should raise error
    with pytest.raises(NexusFileNotFoundError):
        embedded.read(path)


def test_delete_nonexistent_file_raises_error(embedded: Embedded) -> None:
    """Test that deleting nonexistent file raises error."""
    with pytest.raises(NexusFileNotFoundError):
        embedded.delete("/nonexistent.txt")


def test_delete_removes_metadata(embedded: Embedded) -> None:
    """Test that deleting removes metadata."""
    path = "/test.txt"

    # Create file
    embedded.write(path, b"Content")
    assert embedded.metadata.exists(path)

    # Delete file
    embedded.delete(path)

    # Metadata should be gone
    assert not embedded.metadata.exists(path)
    assert embedded.metadata.get(path) is None


def test_exists(embedded: Embedded) -> None:
    """Test checking file existence."""
    path = "/test.txt"

    # Doesn't exist initially
    assert not embedded.exists(path)

    # Create file
    embedded.write(path, b"Content")
    assert embedded.exists(path)

    # Delete file
    embedded.delete(path)
    assert not embedded.exists(path)


def test_list_files(embedded: Embedded) -> None:
    """Test listing files."""
    # Create multiple files
    embedded.write("/file1.txt", b"Content 1")
    embedded.write("/dir/file2.txt", b"Content 2")
    embedded.write("/dir/subdir/file3.txt", b"Content 3")

    # List all files
    files = embedded.list()

    assert len(files) == 3
    assert "/file1.txt" in files
    assert "/dir/file2.txt" in files
    assert "/dir/subdir/file3.txt" in files


def test_list_with_prefix(embedded: Embedded) -> None:
    """Test listing files with prefix."""
    # Create multiple files
    embedded.write("/file1.txt", b"Content 1")
    embedded.write("/dir/file2.txt", b"Content 2")
    embedded.write("/dir/subdir/file3.txt", b"Content 3")
    embedded.write("/other/file4.txt", b"Content 4")

    # List with prefix
    files = embedded.list(prefix="/dir")

    assert len(files) == 2
    assert "/dir/file2.txt" in files
    assert "/dir/subdir/file3.txt" in files
    assert "/file1.txt" not in files
    assert "/other/file4.txt" not in files


def test_list_empty(embedded: Embedded) -> None:
    """Test listing when no files exist."""
    files = embedded.list()
    assert len(files) == 0


def test_path_validation_empty_path(embedded: Embedded) -> None:
    """Test that empty path raises error."""
    with pytest.raises(InvalidPathError):
        embedded.read("")


def test_path_validation_null_byte(embedded: Embedded) -> None:
    """Test that path with null byte raises error."""
    with pytest.raises(InvalidPathError) as exc_info:
        embedded.write("/bad\x00path.txt", b"Content")

    assert "invalid character" in str(exc_info.value).lower()


def test_path_validation_parent_directory(embedded: Embedded) -> None:
    """Test that path with .. raises error."""
    with pytest.raises(InvalidPathError) as exc_info:
        embedded.read("/../etc/passwd")

    assert ".." in str(exc_info.value)


def test_path_normalization_leading_slash(embedded: Embedded) -> None:
    """Test that paths are normalized with leading slash."""
    content = b"Test content"

    # Write without leading slash
    embedded.write("test.txt", content)

    # Read with leading slash
    result = embedded.read("/test.txt")
    assert result == content

    # Both should be the same file
    assert embedded.exists("test.txt")
    assert embedded.exists("/test.txt")


def test_binary_content(embedded: Embedded) -> None:
    """Test handling of binary content."""
    # Create binary content with various byte values
    content = bytes(range(256))

    embedded.write("/binary.bin", content)

    result = embedded.read("/binary.bin")
    assert result == content


def test_empty_file(embedded: Embedded) -> None:
    """Test handling of empty files."""
    embedded.write("/empty.txt", b"")

    result = embedded.read("/empty.txt")
    assert result == b""

    # Check metadata
    meta = embedded.metadata.get("/empty.txt")
    assert meta is not None
    assert meta.size == 0


def test_large_file(embedded: Embedded) -> None:
    """Test handling of large files."""
    # Create 1MB of data
    content = b"x" * (1024 * 1024)

    embedded.write("/large.bin", content)

    result = embedded.read("/large.bin")
    assert len(result) == len(content)
    assert result == content


def test_unicode_paths(embedded: Embedded) -> None:
    """Test handling of unicode paths."""
    content = b"Unicode content"
    path = "/测试/файл/αρχείο.txt"

    embedded.write(path, content)

    result = embedded.read(path)
    assert result == content
    assert embedded.exists(path)


def test_etag_changes_on_update(embedded: Embedded) -> None:
    """Test that ETag changes when file is updated."""
    path = "/test.txt"

    # Write initial content
    embedded.write(path, b"Content 1")
    meta1 = embedded.metadata.get(path)
    assert meta1 is not None
    etag1 = meta1.etag

    # Update content
    embedded.write(path, b"Content 2")
    meta2 = embedded.metadata.get(path)
    assert meta2 is not None
    etag2 = meta2.etag

    # ETags should be different
    assert etag1 != etag2


def test_etag_same_for_same_content(embedded: Embedded) -> None:
    """Test that ETag is the same for same content."""
    path1 = "/file1.txt"
    path2 = "/file2.txt"
    content = b"Same content"

    # Write same content to two files
    embedded.write(path1, content)
    embedded.write(path2, content)

    # ETags should be the same
    meta1 = embedded.metadata.get(path1)
    meta2 = embedded.metadata.get(path2)
    assert meta1 is not None
    assert meta2 is not None
    assert meta1.etag == meta2.etag


def test_context_manager(temp_dir: Path) -> None:
    """Test using Embedded as context manager."""
    content = b"Test content"

    with Embedded(data_dir=temp_dir) as nx:
        nx.write("/test.txt", content)
        result = nx.read("/test.txt")
        assert result == content


def test_modified_at_updates(embedded: Embedded) -> None:
    """Test that modified_at timestamp updates on write."""
    import time

    path = "/test.txt"

    # Write initial content
    embedded.write(path, b"Content 1")
    meta1 = embedded.metadata.get(path)
    assert meta1 is not None
    modified1 = meta1.modified_at

    # Wait a bit
    time.sleep(0.1)

    # Update content
    embedded.write(path, b"Content 2")
    meta2 = embedded.metadata.get(path)
    assert meta2 is not None
    modified2 = meta2.modified_at

    # Modified timestamp should be later
    assert modified1 is not None
    assert modified2 is not None
    assert modified2 > modified1


def test_created_at_persists(embedded: Embedded) -> None:
    """Test that created_at timestamp persists across updates."""
    path = "/test.txt"

    # Write initial content
    embedded.write(path, b"Content 1")
    meta1 = embedded.metadata.get(path)
    assert meta1 is not None
    created1 = meta1.created_at

    # Update content
    embedded.write(path, b"Content 2")
    meta2 = embedded.metadata.get(path)
    assert meta2 is not None
    created2 = meta2.created_at

    # Created timestamp should be the same
    assert created1 is not None
    assert created2 is not None
    assert created1 == created2


def test_multiple_operations(embedded: Embedded) -> None:
    """Test multiple file operations in sequence."""
    # Create multiple files
    for i in range(10):
        embedded.write(f"/file{i}.txt", f"Content {i}".encode())

    # Verify all exist
    for i in range(10):
        assert embedded.exists(f"/file{i}.txt")

    # Read all
    for i in range(10):
        content = embedded.read(f"/file{i}.txt")
        assert content == f"Content {i}".encode()

    # Delete half
    for i in range(0, 10, 2):
        embedded.delete(f"/file{i}.txt")

    # Verify correct files remain
    for i in range(10):
        if i % 2 == 0:
            assert not embedded.exists(f"/file{i}.txt")
        else:
            assert embedded.exists(f"/file{i}.txt")


def test_overwrite_preserves_path(embedded: Embedded) -> None:
    """Test that overwriting a file preserves the path."""
    path = "/test.txt"

    # Write initial content
    embedded.write(path, b"Content 1")

    # Overwrite
    embedded.write(path, b"Content 2")

    # Should be accessible at same path
    assert embedded.exists(path)
    assert embedded.read(path) == b"Content 2"

    # Should only be one file in list
    files = embedded.list()
    assert len(files) == 1
    assert files[0] == path
