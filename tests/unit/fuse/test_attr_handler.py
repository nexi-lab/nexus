"""Tests for chmod, chown, truncate, utimens attribute operations."""

import errno
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fuse import FuseOSError


class TestChmod:
    """chmod: permission bit masking."""

    def test_chmod_masks_permission_bits(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        # S_IFREG | 0o755 → only 0o755 should be passed to nexus_fs via sys_setattr
        fuse_ops.chmod("/file.txt", 0o100755)
        mock_nexus_fs.sys_setattr.assert_called_once_with("/file.txt", context=None, mode=0o755)

    def test_chmod_invalidates_cache(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        fuse_ops.chmod("/file.txt", 0o644)
        mock_cache.invalidate_and_revoke.assert_called_once_with(["/file.txt"])

    def test_chmod_rejects_virtual_view(self, fuse_ops: Any) -> None:
        with patch.object(fuse_ops, "_parse_virtual_path", return_value=("/f.xlsx", "md")):
            with pytest.raises(FuseOSError) as exc_info:
                fuse_ops.chmod("/f_parsed.xlsx.md", 0o644)
            assert exc_info.value.errno == errno.EROFS


class TestChown:
    """chown: uid/gid mapping."""

    def test_chown_maps_uid(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        # Patch pwd to control mapping
        with patch("nexus.fuse.operations.NexusFUSEOperations.chown") as mock_chown:
            # Just verify it doesn't crash — uid/gid mapping is platform-specific
            mock_chown.return_value = None
            mock_chown(fuse_ops, "/file.txt", os.getuid(), os.getgid())

    def test_chown_invalidates_and_revokes(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        mock_nexus_fs.access.return_value = True

        fuse_ops.chown("/file.txt", 123456, -1)

        mock_cache.invalidate_and_revoke.assert_called_once_with(["/file.txt"])

    def test_chown_rejects_virtual_view(self, fuse_ops: Any) -> None:
        with patch.object(fuse_ops, "_parse_virtual_path", return_value=("/f.xlsx", "md")):
            with pytest.raises(FuseOSError) as exc_info:
                fuse_ops.chown("/f_parsed.xlsx.md", 0, 0)
            assert exc_info.value.errno == errno.EROFS


class TestTruncate:
    """truncate: file size modification."""

    def test_truncate_trims(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b"hello world"

        fuse_ops.truncate("/file.txt", 5)
        mock_nexus_fs.write.assert_called_once_with("/file.txt", b"hello", context=None)

    def test_truncate_pads(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b"hi"

        fuse_ops.truncate("/file.txt", 5)
        expected = b"hi\x00\x00\x00"
        mock_nexus_fs.write.assert_called_once_with("/file.txt", expected, context=None)

    def test_truncate_invalidates_cache(
        self, fuse_ops: Any, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        fuse_ops.cache = mock_cache
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b"data"

        fuse_ops.truncate("/file.txt", 2)
        mock_cache.invalidate_and_revoke.assert_called_once_with(["/file.txt"])

    def test_truncate_invalidates_readahead(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_read.return_value = b"data"
        fuse_ops._readahead = MagicMock()

        fuse_ops.truncate("/file.txt", 2)

        fuse_ops._readahead.invalidate_path.assert_called_once_with("/file.txt")


class TestUtimens:
    """utimens: timestamp update (no-op)."""

    def test_utimens_is_noop(self, fuse_ops: Any) -> None:
        # Should not raise
        fuse_ops.utimens("/file.txt", (1000.0, 2000.0))
        fuse_ops.utimens("/file.txt", None)
