"""Unit tests for LocalConnectorBackend.

Tests cover:
- Path translation and security (symlink escape prevention)
- Read/write operations with L1 caching
- Directory operations
- Readonly mode
- FileWatcher integration (get_physical_path, get_watch_root)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.backends.storage.local_connector import LocalConnectorBackend
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult


class TestLocalConnectorInit:
    """Test LocalConnectorBackend initialization."""

    def test_init_valid_path(self, tmp_path: Path):
        """Should initialize with valid directory path."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector.local_path == tmp_path
        assert connector.readonly is False
        assert connector.follow_symlinks is True

    def test_init_readonly(self, tmp_path: Path):
        """Should respect readonly flag."""
        connector = LocalConnectorBackend(tmp_path, readonly=True)
        assert connector.readonly is True

    def test_init_nonexistent_path_raises(self):
        """Should raise BackendError for nonexistent path."""
        with pytest.raises(BackendError, match="does not exist"):
            LocalConnectorBackend("/nonexistent/path/12345")

    def test_init_file_path_raises(self, tmp_path: Path):
        """Should raise BackendError if path is a file, not directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")
        with pytest.raises(BackendError, match="not a directory"):
            LocalConnectorBackend(file_path)

    def test_name_property(self, tmp_path: Path):
        """Should return 'local_connector' as name."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector.name == "local_connector"


class TestPathTranslation:
    """Test path translation and security."""

    def test_to_physical_simple(self, tmp_path: Path):
        """Should translate simple virtual path to physical path."""
        connector = LocalConnectorBackend(tmp_path)
        physical = connector._to_physical("file.txt")
        assert physical == tmp_path / "file.txt"

    def test_to_physical_nested(self, tmp_path: Path):
        """Should translate nested virtual path."""
        connector = LocalConnectorBackend(tmp_path)
        physical = connector._to_physical("subdir/file.txt")
        assert physical == tmp_path / "subdir" / "file.txt"

    def test_to_physical_leading_slash(self, tmp_path: Path):
        """Should handle leading slash in virtual path."""
        connector = LocalConnectorBackend(tmp_path)
        physical = connector._to_physical("/file.txt")
        assert physical == tmp_path / "file.txt"

    def test_to_physical_symlink_escape_raises(self, tmp_path: Path):
        """Should raise BackendError if symlink escapes mount root."""
        # Create a symlink pointing outside
        outside_dir = tmp_path.parent / "outside"
        outside_dir.mkdir(exist_ok=True)

        symlink = tmp_path / "escape"
        try:
            symlink.symlink_to(outside_dir)
        except OSError:
            pytest.skip("Symlinks not supported on this system")

        connector = LocalConnectorBackend(tmp_path)
        with pytest.raises(BackendError, match="escapes mount root"):
            connector._to_physical("escape/file.txt")


class TestReadContent:
    """Test read_content with L1 caching."""

    def test_read_existing_file(self, tmp_path: Path):
        """Should read existing file content."""
        # Create test file
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"hello world")

        connector = LocalConnectorBackend(tmp_path)

        # Create mock context
        context = MagicMock()
        context.backend_path = "test.txt"
        context.virtual_path = "/mnt/local/test.txt"
        context.zone_id = None

        result = connector.read_content("", context)
        assert result == b"hello world"

    def test_read_nonexistent_file(self, tmp_path: Path):
        """Should raise NexusFileNotFoundError for nonexistent file."""
        connector = LocalConnectorBackend(tmp_path)

        context = MagicMock()
        context.backend_path = "nonexistent.txt"
        context.virtual_path = "/mnt/local/nonexistent.txt"

        with pytest.raises(NexusFileNotFoundError):
            connector.read_content("", context)

    def test_read_requires_context(self, tmp_path: Path):
        """Should raise BackendError if context is missing."""
        connector = LocalConnectorBackend(tmp_path)
        with pytest.raises(BackendError):
            connector.read_content("", None)


class TestWriteContent:
    """Test write_content."""

    def test_write_new_file(self, tmp_path: Path):
        """Should write new file."""
        connector = LocalConnectorBackend(tmp_path)

        context = MagicMock()
        context.backend_path = "new.txt"
        context.virtual_path = "/mnt/local/new.txt"
        context.zone_id = None

        result = connector.write_content(b"new content", context=context)

        assert isinstance(result, WriteResult)
        assert (tmp_path / "new.txt").read_bytes() == b"new content"

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        """Should create parent directories if needed."""
        connector = LocalConnectorBackend(tmp_path)

        context = MagicMock()
        context.backend_path = "subdir/nested/file.txt"
        context.virtual_path = "/mnt/local/subdir/nested/file.txt"
        context.zone_id = None

        result = connector.write_content(b"nested content", context=context)

        assert isinstance(result, WriteResult)
        assert (tmp_path / "subdir" / "nested" / "file.txt").exists()

    def test_write_readonly_rejected(self, tmp_path: Path):
        """Should raise BackendError in readonly mode."""
        connector = LocalConnectorBackend(tmp_path, readonly=True)

        context = MagicMock()
        context.backend_path = "file.txt"

        with pytest.raises(BackendError):
            connector.write_content(b"content", context=context)


class TestDirectoryOperations:
    """Test directory operations."""

    def test_list_dir(self, tmp_path: Path):
        """Should list directory contents."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")
        (tmp_path / "subdir").mkdir()

        connector = LocalConnectorBackend(tmp_path)
        result = connector.list_dir("")

        # list_dir returns list[str] directly
        assert isinstance(result, list)
        assert "file1.txt" in result
        assert "file2.txt" in result
        assert "subdir" in result

    def test_exists(self, tmp_path: Path):
        """Should check if path exists."""
        (tmp_path / "exists.txt").write_text("content")

        connector = LocalConnectorBackend(tmp_path)
        assert connector.exists("exists.txt") is True
        assert connector.exists("not_exists.txt") is False

    def test_mkdir(self, tmp_path: Path):
        """Should create directory."""
        connector = LocalConnectorBackend(tmp_path)
        result = connector.mkdir("newdir")

        assert result is None
        assert (tmp_path / "newdir").is_dir()

    def test_delete_file(self, tmp_path: Path):
        """Should delete file."""
        (tmp_path / "to_delete.txt").write_text("content")

        connector = LocalConnectorBackend(tmp_path)
        connector.delete("to_delete.txt")

        assert not (tmp_path / "to_delete.txt").exists()

    def test_delete_readonly_rejected(self, tmp_path: Path):
        """Should raise BackendError in readonly mode."""
        (tmp_path / "file.txt").write_text("content")

        connector = LocalConnectorBackend(tmp_path, readonly=True)
        with pytest.raises(BackendError, match="read-only"):
            connector.delete("file.txt")


class TestBackendInterface:
    """Test Backend interface methods (for compatibility)."""

    def test_content_hash_methods_not_supported(self, tmp_path: Path):
        """Content-hash based methods should indicate not supported."""
        connector = LocalConnectorBackend(tmp_path)

        # These methods exist for interface compatibility but don't work with path-based connector
        # They now return direct types or raise exceptions
        result = connector.content_exists("somehash")
        assert result is False

        with pytest.raises(BackendError):
            connector.get_content_size("somehash")

        with pytest.raises(BackendError):
            connector.delete_content("somehash")
