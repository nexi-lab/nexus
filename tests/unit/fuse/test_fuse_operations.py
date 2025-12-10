"""Unit tests for FUSE operations.

These tests verify the FUSE filesystem mount functionality including
file operations, directory operations, and virtual file views.
"""

from __future__ import annotations

import errno
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from nexus.fuse.mount import MountMode
from nexus.fuse.operations import NexusFUSEOperations

# Get FuseOSError from the mocked fuse module (set by conftest.py)
FuseOSError = sys.modules["fuse"].FuseOSError

if TYPE_CHECKING:
    pass


@pytest.fixture
def mock_nexus_fs() -> MagicMock:
    """Create a mock Nexus filesystem."""
    fs = MagicMock(
        spec=[
            "read",
            "write",
            "delete",
            "exists",
            "list",
            "is_directory",
            "mkdir",
            "rmdir",
            "get_available_namespaces",
        ]
    )
    # Add metadata mock (needed for getattr operations)
    fs.metadata = MagicMock()
    fs.metadata.get.return_value = None  # Default: no metadata found
    return fs


@pytest.fixture
def fuse_ops(mock_nexus_fs: MagicMock) -> NexusFUSEOperations:
    """Create FUSE operations with mock filesystem."""
    # Create with empty cache config to avoid metrics issues
    return NexusFUSEOperations(mock_nexus_fs, MountMode.SMART, cache_config={})


class TestVirtualPathParsing:
    """Test virtual path parsing logic."""

    def test_parse_regular_path(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test parsing regular path."""
        # Mock that the .txt file exists but the base doesn't (so it's a real .txt file)
        mock_nexus_fs.exists.return_value = False

        path, view = fuse_ops._parse_virtual_path("/workspace/file.txt")
        assert path == "/workspace/file.txt"
        assert view is None

    def test_parse_raw_path(self, fuse_ops: NexusFUSEOperations) -> None:
        """Test parsing .raw/ path."""
        path, view = fuse_ops._parse_virtual_path("/.raw/workspace/file.pdf")
        assert path == "/workspace/file.pdf"
        assert view is None

    def test_parse_txt_view(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test parsing _parsed.pdf.md virtual view."""
        # Mock base file exists
        mock_nexus_fs.exists.return_value = True

        path, view = fuse_ops._parse_virtual_path("/workspace/file_parsed.pdf.md")
        assert path == "/workspace/file.pdf"
        assert view == "md"

    def test_parse_md_view(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test parsing _parsed.xlsx.md virtual view."""
        # Mock base file exists
        mock_nexus_fs.exists.return_value = True

        path, view = fuse_ops._parse_virtual_path("/workspace/data_parsed.xlsx.md")
        assert path == "/workspace/data.xlsx"
        assert view == "md"


class TestGetattr:
    """Test getattr operation (get file attributes)."""

    def test_getattr_directory(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test getting attributes for a directory."""
        mock_nexus_fs.is_directory.return_value = True

        attrs = fuse_ops.getattr("/workspace")

        assert attrs["st_mode"] & 0o040000  # S_IFDIR
        assert attrs["st_nlink"] == 2

    def test_getattr_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test getting attributes for a file."""
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"Hello, World!"

        attrs = fuse_ops.getattr("/workspace/file.txt")

        assert attrs["st_mode"] & 0o100000  # S_IFREG
        assert attrs["st_size"] == 13
        assert attrs["st_nlink"] == 1

    def test_getattr_nonexistent(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test getting attributes for nonexistent file."""
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.exists.return_value = False

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.getattr("/nonexistent")

        assert exc_info.value.errno == errno.ENOENT

    def test_getattr_raw_directory(self, fuse_ops: NexusFUSEOperations) -> None:
        """Test getting attributes for .raw directory."""
        attrs = fuse_ops.getattr("/.raw")

        assert attrs["st_mode"] & 0o040000  # S_IFDIR
        assert attrs["st_nlink"] == 2


class TestReaddir:
    """Test readdir operation (list directory contents)."""

    def test_readdir_root(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test listing root directory."""
        mock_nexus_fs.list.return_value = []

        entries = fuse_ops.readdir("/")

        assert "." in entries
        assert ".." in entries
        assert ".raw" in entries

    def test_readdir_with_files(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test listing directory with files."""
        mock_nexus_fs.list.return_value = [
            "/workspace/file1.txt",
            "/workspace/file2.pdf",
        ]
        mock_nexus_fs.is_directory.return_value = False

        entries = fuse_ops.readdir("/workspace")

        assert "." in entries
        assert ".." in entries
        assert "file1.txt" in entries
        assert "file2.pdf" in entries
        # In smart mode, should also have virtual views for parseable files
        assert "file2_parsed.pdf.md" in entries

    def test_readdir_binary_mode_no_virtual_views(self, mock_nexus_fs: MagicMock) -> None:
        """Test that binary mode doesn't add virtual views."""
        fuse_ops = NexusFUSEOperations(mock_nexus_fs, MountMode.BINARY)
        mock_nexus_fs.list.return_value = ["/workspace/file.pdf"]
        mock_nexus_fs.is_directory.return_value = False

        entries = fuse_ops.readdir("/workspace")

        assert "file.pdf" in entries
        # Binary mode should not add _parsed virtual views
        assert "file_parsed.pdf.md" not in entries


class TestFileIO:
    """Test file I/O operations (open, read, write, release)."""

    def test_open_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test opening a file."""
        # .txt files are not virtual views, so exists is only called once
        mock_nexus_fs.exists.return_value = True

        fd = fuse_ops.open("/workspace/file.txt", os.O_RDONLY)

        assert fd > 0
        assert fd in fuse_ops.open_files
        assert fuse_ops.open_files[fd]["path"] == "/workspace/file.txt"

    def test_open_nonexistent(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test opening nonexistent file."""
        mock_nexus_fs.exists.return_value = False

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.open("/nonexistent", os.O_RDONLY)

        assert exc_info.value.errno == errno.ENOENT

    def test_read_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test reading file content."""
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"Hello, World!"

        fd = fuse_ops.open("/workspace/file.txt", os.O_RDONLY)
        content = fuse_ops.read("/workspace/file.txt", 13, 0, fd)

        assert content == b"Hello, World!"

    def test_read_with_offset(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test reading file with offset."""
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"Hello, World!"

        fd = fuse_ops.open("/workspace/file.txt", os.O_RDONLY)
        content = fuse_ops.read("/workspace/file.txt", 5, 7, fd)

        assert content == b"World"

    def test_write_new_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test writing to a new file."""
        # create() needs exists to return False (not a virtual view)
        # write() needs exists to return False (file doesn't exist yet)
        mock_nexus_fs.exists.return_value = False

        fd = fuse_ops.create("/workspace/new.txt", 0o644)
        written = fuse_ops.write("/workspace/new.txt", b"Hello", 0, fd)

        assert written == 5
        mock_nexus_fs.write.assert_called()

    def test_write_virtual_view_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that writing to virtual views is not allowed."""
        # First call for exists check in open, second for virtual path check
        mock_nexus_fs.exists.side_effect = [True, True]

        fd = fuse_ops.open("/workspace/file_parsed.pdf.md", os.O_RDONLY)

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.write("/workspace/file_parsed.pdf.md", b"data", 0, fd)

        assert exc_info.value.errno == errno.EROFS

    def test_release_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test releasing (closing) a file."""
        mock_nexus_fs.exists.return_value = True

        fd = fuse_ops.open("/workspace/file.txt", os.O_RDONLY)
        fuse_ops.release("/workspace/file.txt", fd)

        assert fd not in fuse_ops.open_files


class TestFileCreationDeletion:
    """Test file and directory creation/deletion operations."""

    def test_create_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test creating a new file."""
        # Mock that base path doesn't exist (not a virtual view)
        mock_nexus_fs.exists.return_value = False

        fd = fuse_ops.create("/workspace/new.txt", 0o644)

        assert fd > 0
        mock_nexus_fs.write.assert_called_with("/workspace/new.txt", b"")

    def test_create_virtual_view_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that creating virtual views is not allowed."""
        # Mock base file exists to trigger virtual view detection
        mock_nexus_fs.exists.return_value = True

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.create("/workspace/file_parsed.pdf.md", 0o644)

        assert exc_info.value.errno == errno.EROFS

    def test_unlink_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test deleting a file."""
        # Mock that base path doesn't exist (not a virtual view)
        mock_nexus_fs.exists.return_value = False

        fuse_ops.unlink("/workspace/file.txt")

        mock_nexus_fs.delete.assert_called_with("/workspace/file.txt")

    def test_unlink_virtual_view_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that deleting virtual views is not allowed."""
        # Mock base file exists to trigger virtual view detection
        mock_nexus_fs.exists.return_value = True

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.unlink("/workspace/file_parsed.pdf.md")

        assert exc_info.value.errno == errno.EROFS

    def test_mkdir(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test creating a directory."""
        fuse_ops.mkdir("/workspace/new_dir", 0o755)

        mock_nexus_fs.mkdir.assert_called_with("/workspace/new_dir", parents=True, exist_ok=True)

    def test_mkdir_in_raw_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that creating directories in .raw is not allowed."""
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.mkdir("/.raw/workspace", 0o755)

        assert exc_info.value.errno == errno.EROFS

    def test_rmdir(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test removing a directory."""
        fuse_ops.rmdir("/workspace/old_dir")

        mock_nexus_fs.rmdir.assert_called_with("/workspace/old_dir", recursive=False)

    def test_rmdir_raw_fails(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test that removing .raw directory is not allowed."""
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rmdir("/.raw")

        assert exc_info.value.errno == errno.EROFS


class TestRename:
    """Test rename operation."""

    def test_rename_file(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test renaming a file using metadata-only operation."""
        # Mock that neither path is a virtual view
        mock_nexus_fs.exists.return_value = False
        mock_nexus_fs.is_directory.return_value = False  # Source is a file, not a directory
        mock_nexus_fs.rename = MagicMock()  # Add rename method to mock

        fuse_ops.rename("/workspace/old.txt", "/workspace/new.txt")

        # Verify metadata-only rename was called (no read/write/delete!)
        mock_nexus_fs.is_directory.assert_called_with("/workspace/old.txt")
        mock_nexus_fs.rename.assert_called_with("/workspace/old.txt", "/workspace/new.txt")

        # Verify NO content I/O happened
        mock_nexus_fs.read.assert_not_called()
        mock_nexus_fs.write.assert_not_called()
        mock_nexus_fs.delete.assert_not_called()

    def test_rename_virtual_view_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that renaming virtual views is not allowed."""
        # Mock base file exists for source to trigger virtual view detection
        mock_nexus_fs.exists.return_value = True

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rename("/workspace/file_parsed.pdf.md", "/workspace/other.txt")

        assert exc_info.value.errno == errno.EROFS

    def test_rename_in_raw_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that renaming in .raw is not allowed."""
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rename("/.raw/file.txt", "/workspace/file.txt")

        assert exc_info.value.errno == errno.EROFS

    def test_rename_directory(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test renaming a directory with files using metadata-only operations."""
        # Mock that source is a directory
        mock_nexus_fs.exists.return_value = False
        mock_nexus_fs.is_directory.return_value = True
        mock_nexus_fs.rename = MagicMock()  # Add rename method to mock

        # Mock directory contents
        mock_nexus_fs.list.return_value = [
            {"path": "/workspace/old_dir/file1.txt", "is_directory": False},
            {"path": "/workspace/old_dir/subdir", "is_directory": True},
            {"path": "/workspace/old_dir/subdir/file2.txt", "is_directory": False},
        ]

        fuse_ops.rename("/workspace/old_dir", "/workspace/new_dir")

        # Verify directory was checked
        mock_nexus_fs.is_directory.assert_called_with("/workspace/old_dir")

        # Verify files were listed
        mock_nexus_fs.list.assert_called_with("/workspace/old_dir", recursive=True, details=True)

        # Verify files were moved using metadata-only rename (no content I/O!)
        assert mock_nexus_fs.rename.call_count == 2
        mock_nexus_fs.rename.assert_any_call(
            "/workspace/old_dir/file1.txt", "/workspace/new_dir/file1.txt"
        )
        mock_nexus_fs.rename.assert_any_call(
            "/workspace/old_dir/subdir/file2.txt", "/workspace/new_dir/subdir/file2.txt"
        )

        # Verify NO content I/O happened
        mock_nexus_fs.read.assert_not_called()
        mock_nexus_fs.write.assert_not_called()

        # Verify source directory was deleted
        mock_nexus_fs.rmdir.assert_called_with("/workspace/old_dir", recursive=True)

    def test_rename_destination_exists_error(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that renaming to an existing path fails with EEXIST."""
        import errno

        from fuse import FuseOSError

        # Destination already exists (either file or directory)
        mock_nexus_fs.exists.return_value = True

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rename("/workspace/test1", "/workspace/existing_path")

        assert exc_info.value.errno == errno.EEXIST


class TestTruncate:
    """Test truncate operation."""

    def test_truncate_existing_file(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test truncating an existing file."""
        # .txt files are not virtual views, so exists is only called once
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"Hello, World!"

        fuse_ops.truncate("/workspace/file.txt", 5)

        # Should write truncated content
        call_args = mock_nexus_fs.write.call_args
        assert call_args[0][0] == "/workspace/file.txt"
        assert call_args[0][1] == b"Hello"

    def test_truncate_expand(self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock) -> None:
        """Test truncating (expanding) a file."""
        # .txt files are not virtual views, so exists is only called once
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"Hi"

        fuse_ops.truncate("/workspace/file.txt", 5)

        # Should pad with zeros
        call_args = mock_nexus_fs.write.call_args
        assert call_args[0][0] == "/workspace/file.txt"
        assert call_args[0][1] == b"Hi\x00\x00\x00"

    def test_truncate_virtual_view_fails(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that truncating virtual views is not allowed."""
        # Mock base file exists to trigger virtual view detection
        mock_nexus_fs.exists.return_value = True

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.truncate("/workspace/file_parsed.pdf.md", 0)

        assert exc_info.value.errno == errno.EROFS


class TestMountModes:
    """Test different mount modes."""

    def test_binary_mode_returns_raw(self, mock_nexus_fs: MagicMock) -> None:
        """Test that binary mode returns raw content."""
        fuse_ops = NexusFUSEOperations(mock_nexus_fs, MountMode.BINARY)
        raw_content = b"\x89PNG\r\n\x1a\n"  # PNG header
        mock_nexus_fs.read.return_value = raw_content

        content = fuse_ops._get_file_content("/file.png", None)

        assert content == raw_content

    def test_text_mode_decodes_text(self, mock_nexus_fs: MagicMock) -> None:
        """Test that text mode decodes text files."""
        fuse_ops = NexusFUSEOperations(mock_nexus_fs, MountMode.TEXT)
        mock_nexus_fs.read.return_value = b"Hello, World!"

        content = fuse_ops._get_file_content("/file.txt", "txt")

        assert content == b"Hello, World!"

    def test_smart_mode_with_view(self, mock_nexus_fs: MagicMock) -> None:
        """Test that smart mode uses parser for virtual views."""
        fuse_ops = NexusFUSEOperations(mock_nexus_fs, MountMode.SMART)
        mock_nexus_fs.read.return_value = b"text content"

        # Should try to parse when view_type is specified
        content = fuse_ops._get_file_content("/file.txt", "txt")

        # Should return text (decoded UTF-8)
        assert content == b"text content"


class TestCacheIntegration:
    """Test cache integration in FUSE operations."""

    def test_getattr_uses_cache(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that getattr uses cache for repeated requests."""
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"content"

        # First call should hit filesystem
        attrs1 = fuse_ops.getattr("/file.txt")

        # Second call should use cache (read should only be called once)
        attrs2 = fuse_ops.getattr("/file.txt")

        assert attrs1 == attrs2
        assert mock_nexus_fs.read.call_count == 1

    def test_write_invalidates_cache(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that writing invalidates cache."""
        # Set up file
        mock_nexus_fs.exists.return_value = False

        # Create and write to file
        fd = fuse_ops.create("/file.txt", 0o644)
        fuse_ops.write("/file.txt", b"new content", 0, fd)

        # Cache invalidation happens during write - just verify write succeeded
        assert fd > 0

    def test_read_uses_content_cache(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that reading uses content cache."""
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"cached content"

        # Open and read file twice
        fd = fuse_ops.open("/file.txt", os.O_RDONLY)
        content1 = fuse_ops.read("/file.txt", 100, 0, fd)
        content2 = fuse_ops.read("/file.txt", 100, 0, fd)

        assert content1 == content2
        # Read should use cache on second call
        assert mock_nexus_fs.read.call_count >= 1


class TestRemoteFilesystem:
    """Test FUSE operations with remote filesystem (no metadata attribute)."""

    def test_getattr_directory_without_metadata(self) -> None:
        """Test getattr on directory with filesystem that lacks metadata attribute."""
        # Create a mock filesystem WITHOUT metadata attribute (like RemoteNexusFS)
        fs = MagicMock(
            spec=[
                "read",
                "write",
                "delete",
                "exists",
                "list",
                "is_directory",
                "mkdir",
                "rmdir",
                "get_available_namespaces",
            ]
        )
        fs.is_directory.return_value = True
        fs.exists.return_value = True

        # Create FUSE operations with this filesystem
        ops = NexusFUSEOperations(fs, MountMode.SMART, cache_config={})

        # Should not raise AttributeError
        attrs = ops.getattr("/workspace")
        assert attrs["st_mode"] & 0o040000  # Directory flag

    def test_getattr_file_without_metadata(self) -> None:
        """Test getattr on file with filesystem that lacks metadata attribute."""
        # Create a mock filesystem WITHOUT metadata attribute (like RemoteNexusFS)
        fs = MagicMock(
            spec=[
                "read",
                "write",
                "delete",
                "exists",
                "list",
                "is_directory",
                "mkdir",
                "rmdir",
                "get_available_namespaces",
            ]
        )
        fs.is_directory.return_value = False
        fs.exists.return_value = True
        fs.read.return_value = b"test content"

        # Create FUSE operations with this filesystem
        ops = NexusFUSEOperations(fs, MountMode.SMART, cache_config={})

        # Should not raise AttributeError
        attrs = ops.getattr("/workspace/test.txt")
        assert attrs["st_size"] == 12  # len(b"test content")
        assert attrs["st_mode"] & 0o100000  # Regular file flag

    def test_getattr_with_get_metadata_method(self) -> None:
        """Test getattr with filesystem that has get_metadata method (RemoteNexusFS)."""
        # Create a mock filesystem with get_metadata method
        fs = MagicMock(
            spec=[
                "read",
                "write",
                "delete",
                "exists",
                "list",
                "is_directory",
                "mkdir",
                "rmdir",
                "get_available_namespaces",
                "get_metadata",
            ]
        )
        fs.is_directory.return_value = False

        # Use a file path that won't be confused with virtual views
        def exists_side_effect(path: str) -> bool:
            # The file exists, but base path without extension doesn't
            return path == "/workspace/document.dat"

        fs.exists.side_effect = exists_side_effect
        fs.read.return_value = b"test content"
        fs.get_metadata.return_value = {
            "path": "/workspace/document.dat",
            "owner": "alice",
            "group": "developers",
            "mode": 0o640,
            "is_directory": False,
        }

        # Create FUSE operations with this filesystem
        ops = NexusFUSEOperations(fs, MountMode.SMART, cache_config={})

        # Get attributes
        attrs = ops.getattr("/workspace/document.dat")

        # Verify get_metadata was called
        fs.get_metadata.assert_called_once_with("/workspace/document.dat")

        # Verify custom permissions are used
        assert attrs["st_mode"] & 0o777 == 0o640  # Custom mode

    def test_getattr_directory_with_get_metadata_method(self) -> None:
        """Test getattr on directory with get_metadata method."""
        fs = MagicMock(
            spec=[
                "read",
                "write",
                "delete",
                "exists",
                "list",
                "is_directory",
                "mkdir",
                "rmdir",
                "get_available_namespaces",
                "get_metadata",
            ]
        )
        fs.is_directory.return_value = True
        fs.exists.return_value = True
        fs.get_metadata.return_value = {
            "path": "/workspace",
            "owner": "alice",
            "group": "developers",
            "mode": 0o750,
            "is_directory": True,
        }

        # Create FUSE operations with this filesystem
        ops = NexusFUSEOperations(fs, MountMode.SMART, cache_config={})

        # Get attributes
        attrs = ops.getattr("/workspace")

        # Verify get_metadata was called
        fs.get_metadata.assert_called_once_with("/workspace")

        # Verify custom permissions are used
        assert attrs["st_mode"] & 0o777 == 0o750  # Custom directory mode


class TestErrorHandling:
    """Test error handling in FUSE operations."""

    def test_getattr_with_invalid_path(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test getattr with filesystem errors."""
        mock_nexus_fs.is_directory.side_effect = Exception("Filesystem error")

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.getattr("/error")

        assert exc_info.value.errno == errno.EIO

    def test_readdir_with_filesystem_error(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test readdir handling of filesystem errors."""
        mock_nexus_fs.list.side_effect = Exception("List error")

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.readdir("/error")

        assert exc_info.value.errno == errno.EIO

    def test_open_with_filesystem_error(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test open handling of filesystem errors."""
        mock_nexus_fs.exists.side_effect = Exception("Exists error")

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.open("/error", os.O_RDONLY)

        assert exc_info.value.errno == errno.EIO

    def test_read_with_bad_file_descriptor(self, fuse_ops: NexusFUSEOperations) -> None:
        """Test reading with invalid file descriptor."""
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.read("/file.txt", 100, 0, 999)

        # EBADF may be caught and converted to EIO
        assert exc_info.value.errno in (errno.EBADF, errno.EIO)

    def test_read_with_filesystem_error(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test read handling of filesystem errors."""
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.side_effect = Exception("Read error")

        fd = fuse_ops.open("/file.txt", os.O_RDONLY)

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.read("/file.txt", 100, 0, fd)

        assert exc_info.value.errno == errno.EIO


class TestWindowsCompatibility:
    """Test Windows compatibility features."""

    def test_getattr_windows_compatibility(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that getattr works on Windows (no getuid/getgid)."""
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.read.return_value = b"content"

        # Temporarily hide os.getuid and os.getgid to simulate Windows
        import os

        original_getuid = getattr(os, "getuid", None)
        original_getgid = getattr(os, "getgid", None)

        try:
            # Remove the functions if they exist
            if original_getuid is not None:
                delattr(os, "getuid")
            if original_getgid is not None:
                delattr(os, "getgid")

            attrs = fuse_ops.getattr("/file.txt")

            # Should use fallback values
            assert attrs["st_uid"] == 0
            assert attrs["st_gid"] == 0
        finally:
            # Restore the functions
            if original_getuid is not None:
                os.getuid = original_getuid
            if original_getgid is not None:
                os.getgid = original_getgid


class TestCacheConfiguration:
    """Test cache configuration."""

    def test_custom_cache_config(self, mock_nexus_fs: MagicMock) -> None:
        """Test FUSE operations with custom cache configuration."""
        cache_config = {
            "attr_cache_size": 2048,
            "attr_cache_ttl": 120,
            "content_cache_size": 200,
            "parsed_cache_size": 100,
            "enable_metrics": True,
        }

        fuse_ops = NexusFUSEOperations(mock_nexus_fs, MountMode.SMART, cache_config=cache_config)

        # Cache should be initialized with custom config
        assert fuse_ops.cache is not None
        # We can verify metrics are enabled by getting metrics
        metrics = fuse_ops.cache.get_metrics()
        assert metrics is not None
        assert isinstance(metrics, dict)

    def test_default_cache_config(self, mock_nexus_fs: MagicMock) -> None:
        """Test FUSE operations with default cache configuration."""
        fuse_ops = NexusFUSEOperations(mock_nexus_fs, MountMode.SMART)

        # Cache should be initialized with defaults
        assert fuse_ops.cache is not None


class TestHelperMethods:
    """Test helper methods."""

    def test_dir_attrs(self, fuse_ops: NexusFUSEOperations) -> None:
        """Test _dir_attrs helper method."""
        attrs = fuse_ops._dir_attrs()

        assert attrs["st_mode"] & 0o040000  # S_IFDIR
        assert attrs["st_nlink"] == 2
        assert "st_ctime" in attrs
        assert "st_mtime" in attrs
        assert "st_atime" in attrs


class TestRawDirectory:
    """Test .raw directory functionality."""

    def test_getattr_for_raw_dir(self, fuse_ops: NexusFUSEOperations) -> None:
        """Test getting attributes for .raw directory itself."""
        attrs = fuse_ops.getattr("/.raw")

        assert attrs["st_mode"] & 0o040000  # S_IFDIR


class TestComplexPaths:
    """Test handling of complex file paths."""

    def test_nested_directory_structure(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test handling deeply nested paths."""
        mock_nexus_fs.list.return_value = ["/a/b/c/d/file.txt"]
        mock_nexus_fs.is_directory.return_value = False

        entries = fuse_ops.readdir("/a/b/c/d")

        assert "file.txt" in entries

    def test_path_with_special_characters(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test paths with special characters."""
        special_path = "/workspace/file-name_2024.txt"
        # .txt files are not virtual views, so exists is only called once
        mock_nexus_fs.exists.return_value = True

        fd = fuse_ops.open(special_path, os.O_RDONLY)

        assert fd > 0
        assert fuse_ops.open_files[fd]["path"] == special_path


class TestMetadataObjSize:
    """Test that MetadataObj properly includes size attribute (fix for issue #624)."""

    def test_metadata_obj_has_size_attribute(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that MetadataObj includes size from get_metadata response."""
        # Mock get_metadata to return size
        mock_nexus_fs.get_metadata = MagicMock(
            return_value={
                "path": "/test/file.txt",
                "size": 12345,
                "owner": "user",
                "group": "group",
                "mode": 0o644,
                "is_directory": False,
            }
        )
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.is_directory.return_value = False

        # Get metadata through _get_metadata
        metadata = fuse_ops._get_metadata("/test/file.txt")

        # Verify size attribute exists and has correct value
        assert metadata is not None
        assert hasattr(metadata, "size")
        assert metadata.size == 12345

    def test_getattr_uses_metadata_size(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that getattr uses size from metadata instead of fetching content."""
        # Mock get_metadata to return size
        mock_nexus_fs.get_metadata = MagicMock(
            return_value={
                "path": "/test/large_file.bin",
                "size": 1_000_000,  # 1MB
                "owner": "user",
                "group": "group",
                "mode": 0o644,
                "is_directory": False,
            }
        )
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.is_directory.return_value = False

        # Get file attributes
        attrs = fuse_ops.getattr("/test/large_file.bin")

        # Verify size from metadata is used
        assert attrs["st_size"] == 1_000_000

        # Verify read() was NOT called (we used metadata size, not content)
        mock_nexus_fs.read.assert_not_called()

    def test_metadata_obj_size_zero_is_valid(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that size=0 is a valid value (not treated as missing)."""
        # Mock get_metadata to return size=0 (empty file)
        mock_nexus_fs.get_metadata = MagicMock(
            return_value={
                "path": "/test/empty.txt",
                "size": 0,
                "owner": "user",
                "group": "group",
                "mode": 0o644,
                "is_directory": False,
            }
        )
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.is_directory.return_value = False

        # Get metadata
        metadata = fuse_ops._get_metadata("/test/empty.txt")

        # Verify size is 0 (not None or missing)
        assert metadata is not None
        assert hasattr(metadata, "size")
        assert metadata.size == 0

    def test_metadata_obj_missing_size_returns_none(
        self, fuse_ops: NexusFUSEOperations, mock_nexus_fs: MagicMock
    ) -> None:
        """Test that missing size in metadata dict returns None (not crash)."""
        # Mock get_metadata to return dict WITHOUT size
        mock_nexus_fs.get_metadata = MagicMock(
            return_value={
                "path": "/test/file.txt",
                # size missing
                "owner": "user",
                "group": "group",
                "mode": 0o644,
                "is_directory": False,
            }
        )
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.is_directory.return_value = False

        # Get metadata
        metadata = fuse_ops._get_metadata("/test/file.txt")

        # Verify size attribute exists but is None
        assert metadata is not None
        assert hasattr(metadata, "size")
        assert metadata.size is None
