"""Tests for create, unlink, mkdir, rmdir, rename mutation operations."""

import errno
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fuse import FuseOSError


class TestCreate:
    """create: file creation with virtual view rejection."""

    def test_create_returns_fd(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        fd = fuse_ops.create("/new.txt", 0o644)
        assert isinstance(fd, int)
        assert fd > 0
        mock_nexus_fs.write.assert_called_once_with("/new.txt", b"", context=None)

    def test_create_rejects_virtual_view(self, fuse_ops: Any) -> None:
        # _parsed.*.md virtual views should be read-only
        with patch.object(fuse_ops, "_parse_virtual_path", return_value=("/file.xlsx", "md")):
            with pytest.raises(FuseOSError) as exc_info:
                fuse_ops.create("/file_parsed.xlsx.md", 0o644)
            assert exc_info.value.errno == errno.EROFS

    def test_create_rejects_os_metadata(self, fuse_ops: Any) -> None:
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.create("/dir/.DS_Store", 0o644)
        assert exc_info.value.errno == errno.EPERM

    def test_create_invalidates_cache(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        fuse_ops.create("/new.txt", 0o644)
        mock_cache.invalidate_path.assert_called()


class TestUnlink:
    """unlink: file deletion with cache invalidation."""

    def test_unlink_calls_delete(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        fuse_ops.unlink("/file.txt")
        mock_nexus_fs.sys_unlink.assert_called_once_with("/file.txt", context=None)

    def test_unlink_invalidates_cache(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        fuse_ops.unlink("/file.txt")
        mock_cache.invalidate_path.assert_called_with("/file.txt")

    def test_unlink_rejects_virtual_view(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        # Patch _parse_virtual_path to simulate a virtual view
        mock_nexus_fs.sys_access.return_value = True
        # _parsed views: these are handled by parse_virtual_path returning a view_type
        # For simplicity, patch _parse_virtual_path
        with patch.object(fuse_ops, "_parse_virtual_path", return_value=("/file.xlsx", "md")):
            with pytest.raises(FuseOSError) as exc_info:
                fuse_ops.unlink("/file_parsed.xlsx.md")
            assert exc_info.value.errno == errno.EROFS


class TestMkdir:
    """mkdir: directory creation."""

    def test_mkdir_calls_fs(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        fuse_ops.mkdir("/newdir", 0o755)
        mock_nexus_fs.mkdir.assert_called_once_with(
            "/newdir", parents=True, exist_ok=True, context=None
        )

    def test_mkdir_rejects_raw(self, fuse_ops: Any) -> None:
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.mkdir("/.raw/subdir", 0o755)
        assert exc_info.value.errno == errno.EROFS


class TestRmdir:
    """rmdir: directory removal."""

    def test_rmdir_calls_fs(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        fuse_ops.rmdir("/emptydir")
        mock_nexus_fs.sys_rmdir.assert_called_once_with("/emptydir", recursive=False, context=None)

    def test_rmdir_rejects_raw_root(self, fuse_ops: Any) -> None:
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rmdir("/.raw")
        assert exc_info.value.errno == errno.EROFS


class TestRename:
    """rename: file/directory rename with cache invalidation."""

    def test_rename_file(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.sys_access.return_value = False  # dest doesn't exist
        mock_nexus_fs.sys_is_directory.return_value = False

        fuse_ops.rename("/old.txt", "/new.txt")
        mock_nexus_fs.sys_rename.assert_called_once_with("/old.txt", "/new.txt", context=None)

    def test_rename_rejects_existing_dest(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.sys_access.return_value = True

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rename("/old.txt", "/existing.txt")
        assert exc_info.value.errno == errno.EEXIST

    def test_rename_directory(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.sys_access.return_value = False
        mock_nexus_fs.sys_is_directory.return_value = True
        mock_nexus_fs.sys_readdir.return_value = [
            {"path": "/olddir/a.txt", "is_directory": False},
        ]

        fuse_ops.rename("/olddir", "/newdir")
        mock_nexus_fs.sys_rename.assert_called_once_with(
            "/olddir/a.txt", "/newdir/a.txt", context=None
        )
        mock_nexus_fs.sys_rmdir.assert_called_once_with("/olddir", recursive=True, context=None)

    def test_rename_rejects_raw_paths(self, fuse_ops: Any) -> None:
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.rename("/.raw/old", "/new")
        assert exc_info.value.errno == errno.EROFS
