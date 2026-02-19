"""Tests for getattr and readdir metadata operations."""

from __future__ import annotations

import errno
import stat
from typing import Any
from unittest.mock import MagicMock

import pytest
from fuse import FuseOSError


class TestGetattr:
    """getattr: file/directory attribute resolution."""

    def test_cache_hit_returns_cached(self, fuse_ops: Any, mock_cache: MagicMock) -> None:
        cached = {"st_mode": stat.S_IFREG | 0o644, "st_size": 42}
        fuse_ops.cache = mock_cache
        mock_cache.get_attr.return_value = cached

        result = fuse_ops.getattr("/file.txt")
        assert result == cached
        mock_cache.get_attr.assert_called_with("/file.txt")

    def test_root_returns_dir_attrs(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.is_directory.return_value = True
        result = fuse_ops.getattr("/")
        assert result["st_mode"] & stat.S_IFDIR

    def test_directory_returns_dir_attrs(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.is_directory.return_value = True
        mock_nexus_fs.get_metadata.return_value = None
        result = fuse_ops.getattr("/mydir")
        assert result["st_mode"] & stat.S_IFDIR

    def test_file_returns_file_attrs(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.exists.return_value = True
        mock_nexus_fs.get_metadata.return_value = {"size": 1024}

        result = fuse_ops.getattr("/data.bin")
        assert result["st_mode"] & stat.S_IFREG
        assert result["st_size"] == 1024

    def test_missing_file_raises_enoent(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.exists.return_value = False

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.getattr("/nope")
        assert exc_info.value.errno == errno.ENOENT

    def test_raw_dir_returns_dir_attrs(self, fuse_ops: Any) -> None:
        result = fuse_ops.getattr("/.raw")
        assert result["st_mode"] & stat.S_IFDIR


class TestReaddir:
    """readdir: directory listing with caching and virtual views."""

    def test_basic_listing(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.list.return_value = [
            {"path": "/dir/file1.txt", "is_directory": False, "size": 10},
            {"path": "/dir/file2.txt", "is_directory": False, "size": 20},
        ]

        entries = fuse_ops.readdir("/dir")
        assert "." in entries
        assert ".." in entries
        assert "file1.txt" in entries
        assert "file2.txt" in entries

    def test_root_includes_raw(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.list.return_value = []
        entries = fuse_ops.readdir("/")
        assert ".raw" in entries

    def test_os_metadata_filtered(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.list.return_value = [
            {"path": "/dir/.DS_Store", "is_directory": False, "size": 0},
            {"path": "/dir/real.txt", "is_directory": False, "size": 10},
        ]

        entries = fuse_ops.readdir("/dir")
        assert ".DS_Store" not in entries
        assert "real.txt" in entries

    def test_cache_hit_skips_listing(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        """When dir cache has entries, list() should not be called."""
        # Prime the dir cache
        fuse_ops._dir_cache[fuse_ops._dir_cache_key("/cached")] = [".", "..", "a.txt"]

        entries = fuse_ops.readdir("/cached")
        assert entries == [".", "..", "a.txt"]
        mock_nexus_fs.list.assert_not_called()
