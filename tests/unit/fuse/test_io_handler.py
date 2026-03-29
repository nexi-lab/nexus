"""Tests for open, read, write, release I/O operations."""

import errno
import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from fuse import FuseOSError


class TestOpen:
    """open: file descriptor allocation and validation."""

    def test_open_returns_fd(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        fd = fuse_ops.open("/file.txt", os.O_RDONLY)
        assert isinstance(fd, int)
        assert fd > 0

    def test_open_cache_hit_skips_exists(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        mock_cache.get_content.return_value = b"cached"
        mock_cache.get_attr.return_value = None

        fd = fuse_ops.open("/cached.txt", os.O_RDONLY)
        assert fd > 0
        mock_nexus_fs.access.assert_not_called()

    def test_open_missing_file_raises_enoent(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        mock_nexus_fs.access.return_value = False
        mock_cache.get_content.return_value = None
        mock_cache.get_attr.return_value = None

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.open("/missing", os.O_RDONLY)
        assert exc_info.value.errno == errno.ENOENT

    def test_open_increments_fd(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        fd1 = fuse_ops.open("/a", os.O_RDONLY)
        fd2 = fuse_ops.open("/b", os.O_RDONLY)
        assert fd2 > fd1


class TestRead:
    """read: content retrieval with offset slicing."""

    def test_read_from_handle(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b"hello world"
        fd = fuse_ops.open("/file.txt", os.O_RDONLY)

        data = fuse_ops.read("/file.txt", 5, 0, fd)
        assert data == b"hello"

    def test_read_with_offset(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b"hello world"
        fd = fuse_ops.open("/file.txt", os.O_RDONLY)

        data = fuse_ops.read("/file.txt", 5, 6, fd)
        assert data == b"world"

    def test_read_bad_fd_raises_ebadf(self, fuse_ops: Any) -> None:
        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.read("/file.txt", 10, 0, 9999)
        assert exc_info.value.errno == errno.EBADF


class TestWrite:
    """write: content modification with cache invalidation."""

    def test_write_returns_length(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b""
        fd = fuse_ops.open("/file.txt", os.O_RDWR)

        written = fuse_ops.write("/file.txt", b"data", 0, fd)
        assert written == 4

    def test_write_invalidates_cache(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        mock_nexus_fs.access.return_value = True
        fd = fuse_ops.open("/file.txt", os.O_RDWR)
        # Replace cache after open (which uses real cache)
        fuse_ops.cache = mock_cache
        mock_nexus_fs.sys_read.return_value = b""

        fuse_ops.write("/file.txt", b"data", 0, fd)
        # Issue #3397: write now calls invalidate_and_revoke instead of invalidate_path
        mock_cache.invalidate_and_revoke.assert_called()

    def test_write_to_virtual_view_raises_erofs(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock
    ) -> None:
        # Manually insert a file handle with a view_type
        with fuse_ops._files_lock:
            fuse_ops.fd_counter += 1
            fd = fuse_ops.fd_counter
            fuse_ops.open_files[fd] = {
                "path": "/file.xlsx",
                "view_type": "md",
                "flags": os.O_RDONLY,
                "auth_verified": False,
            }

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.write("/file_parsed.xlsx.md", b"data", 0, fd)
        assert exc_info.value.errno == errno.EROFS


class TestRelease:
    """release: cleanup of file descriptors."""

    def test_release_removes_fd(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        fd = fuse_ops.open("/file.txt", os.O_RDONLY)
        assert fd in fuse_ops.open_files

        fuse_ops.release("/file.txt", fd)
        assert fd not in fuse_ops.open_files
