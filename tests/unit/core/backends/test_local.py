"""Unit tests for LocalFS backend."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus.core.backends.local import LocalBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def backend(temp_dir: Path) -> LocalBackend:
    """Create a LocalBackend instance."""
    return LocalBackend(temp_dir)


def test_init_creates_root_directory(temp_dir: Path) -> None:
    """Test that backend creates root directory on init."""
    root = temp_dir / "test-root"
    assert not root.exists()

    LocalBackend(root)
    assert root.exists()
    assert root.is_dir()


def test_write_and_read(backend: LocalBackend) -> None:
    """Test writing and reading a file."""
    content = b"Hello, World!"
    path = "test/file.txt"

    # Write file
    backend.write(path, content)

    # Read file
    result = backend.read(path)
    assert result == content


def test_write_creates_parent_directories(backend: LocalBackend) -> None:
    """Test that write creates parent directories."""
    content = b"Test content"
    path = "deep/nested/directory/file.txt"

    backend.write(path, content)

    result = backend.read(path)
    assert result == content


def test_write_overwrites_existing_file(backend: LocalBackend) -> None:
    """Test that write overwrites existing files."""
    path = "test.txt"

    # Write initial content
    backend.write(path, b"Initial content")
    assert backend.read(path) == b"Initial content"

    # Overwrite with new content
    backend.write(path, b"New content")
    assert backend.read(path) == b"New content"


def test_read_nonexistent_file_raises_error(backend: LocalBackend) -> None:
    """Test that reading nonexistent file raises error."""
    with pytest.raises(NexusFileNotFoundError) as exc_info:
        backend.read("nonexistent.txt")

    assert "nonexistent.txt" in str(exc_info.value)


def test_delete(backend: LocalBackend) -> None:
    """Test deleting a file."""
    path = "test.txt"
    content = b"Test content"

    # Create file
    backend.write(path, content)
    assert backend.exists(path)

    # Delete file
    backend.delete(path)
    assert not backend.exists(path)


def test_delete_nonexistent_file_raises_error(backend: LocalBackend) -> None:
    """Test that deleting nonexistent file raises error."""
    with pytest.raises(NexusFileNotFoundError):
        backend.delete("nonexistent.txt")


def test_exists(backend: LocalBackend) -> None:
    """Test checking file existence."""
    path = "test.txt"

    # File doesn't exist
    assert not backend.exists(path)

    # Create file
    backend.write(path, b"Content")

    # File exists
    assert backend.exists(path)

    # Delete file
    backend.delete(path)

    # File doesn't exist again
    assert not backend.exists(path)


def test_get_size(backend: LocalBackend) -> None:
    """Test getting file size."""
    path = "test.txt"
    content = b"Hello, World!"

    backend.write(path, content)

    size = backend.get_size(path)
    assert size == len(content)


def test_get_size_nonexistent_file_raises_error(backend: LocalBackend) -> None:
    """Test that getting size of nonexistent file raises error."""
    with pytest.raises(NexusFileNotFoundError):
        backend.get_size("nonexistent.txt")


def test_list_directory(backend: LocalBackend) -> None:
    """Test listing files in a directory."""
    # Create some files
    backend.write("dir/file1.txt", b"Content 1")
    backend.write("dir/file2.txt", b"Content 2")
    backend.write("dir/subdir/file3.txt", b"Content 3")

    # List directory
    files = backend.list_directory("dir")

    # Should include all files (including in subdirectories)
    assert len(files) >= 3
    assert any("file1.txt" in f for f in files)
    assert any("file2.txt" in f for f in files)
    assert any("file3.txt" in f for f in files)


def test_list_nonexistent_directory_raises_error(backend: LocalBackend) -> None:
    """Test that listing nonexistent directory raises error."""
    with pytest.raises(NexusFileNotFoundError):
        backend.list_directory("nonexistent")


def test_path_traversal_prevention(backend: LocalBackend) -> None:
    """Test that path traversal attacks are prevented."""
    # Try to escape root directory
    with pytest.raises(BackendError) as exc_info:
        backend.read("../../../etc/passwd")

    assert "escapes root" in str(exc_info.value).lower()


def test_leading_slash_handled_correctly(backend: LocalBackend) -> None:
    """Test that leading slashes are handled correctly."""
    content = b"Test content"

    # Write with leading slash
    backend.write("/test.txt", content)

    # Read without leading slash
    assert backend.read("test.txt") == content

    # Read with leading slash
    assert backend.read("/test.txt") == content


def test_binary_content(backend: LocalBackend) -> None:
    """Test handling of binary content."""
    # Create binary content with various byte values
    content = bytes(range(256))

    backend.write("binary.bin", content)

    result = backend.read("binary.bin")
    assert result == content


def test_empty_file(backend: LocalBackend) -> None:
    """Test handling of empty files."""
    backend.write("empty.txt", b"")

    result = backend.read("empty.txt")
    assert result == b""
    assert backend.get_size("empty.txt") == 0


def test_large_file(backend: LocalBackend) -> None:
    """Test handling of large files."""
    # Create 1MB of data
    content = b"x" * (1024 * 1024)

    backend.write("large.bin", content)

    result = backend.read("large.bin")
    assert len(result) == len(content)
    assert result == content


def test_unicode_filename(backend: LocalBackend) -> None:
    """Test handling of unicode filenames."""
    content = b"Unicode content"
    path = "测试/файл/αρχείο.txt"

    backend.write(path, content)

    result = backend.read(path)
    assert result == content
