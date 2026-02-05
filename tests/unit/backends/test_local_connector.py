"""Unit tests for LocalConnectorBackend.

Tests cover:
- Path translation and security (symlink escape prevention)
- Read/write operations with L1 caching
- Directory operations
- Readonly mode
- FileWatcher integration (get_physical_path, get_watch_root)
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.local_connector import LocalConnectorBackend
from nexus.core.exceptions import BackendError


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

    def test_get_physical_path(self, tmp_path: Path):
        """get_physical_path should return same as _to_physical."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector.get_physical_path("file.txt") == tmp_path / "file.txt"

    def test_get_watch_root(self, tmp_path: Path):
        """get_watch_root should return local_path."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector.get_watch_root() == tmp_path


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
        assert result.success is True
        assert result.data == b"hello world"

    def test_read_nonexistent_file(self, tmp_path: Path):
        """Should return not_found for nonexistent file."""
        connector = LocalConnectorBackend(tmp_path)

        context = MagicMock()
        context.backend_path = "nonexistent.txt"
        context.virtual_path = "/mnt/local/nonexistent.txt"

        result = connector.read_content("", context)
        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_read_requires_context(self, tmp_path: Path):
        """Should return error if context is missing."""
        connector = LocalConnectorBackend(tmp_path)
        result = connector.read_content("", None)
        assert result.success is False
        assert "requires context" in result.error_message

    def test_read_uses_l1_cache(self, tmp_path: Path):
        """Should check L1 cache before reading from disk."""
        test_file = tmp_path / "cached.txt"
        test_file.write_bytes(b"original content")

        connector = LocalConnectorBackend(tmp_path)

        context = MagicMock()
        context.backend_path = "cached.txt"
        context.virtual_path = "/mnt/local/cached.txt"
        context.zone_id = None

        # Mock L1 cache hit
        with patch.object(connector, "_read_from_cache") as mock_cache:
            mock_entry = MagicMock()
            mock_entry.stale = False
            mock_entry.content_binary = b"cached content"
            mock_cache.return_value = mock_entry

            result = connector.read_content("", context)

            # Should return cached content
            assert result.data == b"cached content"
            mock_cache.assert_called_once()


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

        assert result.success is True
        assert (tmp_path / "new.txt").read_bytes() == b"new content"

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        """Should create parent directories if needed."""
        connector = LocalConnectorBackend(tmp_path)

        context = MagicMock()
        context.backend_path = "subdir/nested/file.txt"
        context.virtual_path = "/mnt/local/subdir/nested/file.txt"
        context.zone_id = None

        result = connector.write_content(b"nested content", context=context)

        assert result.success is True
        assert (tmp_path / "subdir" / "nested" / "file.txt").exists()

    def test_write_readonly_rejected(self, tmp_path: Path):
        """Should reject write in readonly mode."""
        connector = LocalConnectorBackend(tmp_path, readonly=True)

        context = MagicMock()
        context.backend_path = "file.txt"

        result = connector.write_content(b"content", context=context)

        assert result.success is False
        assert "read-only" in result.error_message


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

    def test_is_dir(self, tmp_path: Path):
        """Should check if path is directory."""
        (tmp_path / "file.txt").write_text("content")
        (tmp_path / "subdir").mkdir()

        connector = LocalConnectorBackend(tmp_path)
        assert connector.is_dir("subdir") is True
        assert connector.is_dir("file.txt") is False

    def test_mkdir(self, tmp_path: Path):
        """Should create directory."""
        connector = LocalConnectorBackend(tmp_path)
        result = connector.mkdir("newdir")

        assert result.success is True
        assert (tmp_path / "newdir").is_dir()

    def test_delete_file(self, tmp_path: Path):
        """Should delete file."""
        (tmp_path / "to_delete.txt").write_text("content")

        connector = LocalConnectorBackend(tmp_path)
        result = connector.delete("to_delete.txt")

        assert result.success is True
        assert not (tmp_path / "to_delete.txt").exists()

    def test_delete_readonly_rejected(self, tmp_path: Path):
        """Should reject delete in readonly mode."""
        (tmp_path / "file.txt").write_text("content")

        connector = LocalConnectorBackend(tmp_path, readonly=True)
        result = connector.delete("file.txt")

        assert result.success is False
        assert "read-only" in result.error_message


class TestBackendInterface:
    """Test Backend interface methods (for compatibility)."""

    def test_content_hash_methods_not_supported(self, tmp_path: Path):
        """Content-hash based methods should indicate not supported."""
        connector = LocalConnectorBackend(tmp_path)

        # These methods exist for interface compatibility but don't work with path-based connector
        # They now return HandlerResponse for consistency with Backend interface
        result = connector.content_exists("somehash")
        assert result.success is True
        assert result.data is False

        result = connector.get_content_size("somehash")
        assert result.success is False  # Error response

        result = connector.get_ref_count("somehash")
        assert result.success is True
        assert result.data == 0

        result = connector.delete_content("somehash")
        assert result.success is False
        assert "not supported" in result.error_message


class TestCacheConfiguration:
    """Test cache configuration."""

    def test_l1_only_is_true(self, tmp_path: Path):
        """LocalConnectorBackend should have l1_only=True."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector.l1_only is True

    def test_has_virtual_filesystem_is_true(self, tmp_path: Path):
        """LocalConnectorBackend should have has_virtual_filesystem=True."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector.has_virtual_filesystem is True

    def test_has_caching_returns_true(self, tmp_path: Path):
        """_has_caching should return True (L1-only mode)."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector._has_caching() is True

    def test_has_l2_caching_returns_false(self, tmp_path: Path):
        """_has_l2_caching should return False (L1-only mode)."""
        connector = LocalConnectorBackend(tmp_path)
        assert connector._has_l2_caching() is False
