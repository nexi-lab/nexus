"""Tests for getattr and readdir metadata operations."""

import asyncio
import errno
import stat
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fuse import FuseOSError

from nexus.fuse.cache import FUSECacheManager
from nexus.fuse.ops._shared import get_metadata, stat_size_fallback
from nexus.fuse.ops.metadata_handler import (
    _PARSED_SIZE_MULTIPLIER,
    _VIRTUAL_VIEW_DEFAULT_SIZE,
)


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
        mock_nexus_fs.sys_stat.return_value = None
        result = fuse_ops.getattr("/mydir")
        assert result["st_mode"] & stat.S_IFDIR

    def test_file_returns_file_attrs(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.access.return_value = True
        mock_nexus_fs.sys_stat.return_value = {"size": 1024}

        result = fuse_ops.getattr("/data.bin")
        assert result["st_mode"] & stat.S_IFREG
        assert result["st_size"] == 1024

    def test_missing_file_raises_enoent(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.is_directory.return_value = False
        mock_nexus_fs.access.return_value = False

        with pytest.raises(FuseOSError) as exc_info:
            fuse_ops.getattr("/nope")
        assert exc_info.value.errno == errno.ENOENT

    def test_raw_dir_returns_dir_attrs(self, fuse_ops: Any) -> None:
        result = fuse_ops.getattr("/.raw")
        assert result["st_mode"] & stat.S_IFDIR


class TestReaddir:
    """readdir: directory listing with caching and virtual views."""

    def test_basic_listing(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.sys_readdir.return_value = [
            {"path": "/dir/file1.txt", "is_directory": False, "size": 10},
            {"path": "/dir/file2.txt", "is_directory": False, "size": 20},
        ]

        entries = fuse_ops.readdir("/dir")
        assert "." in entries
        assert ".." in entries
        assert "file1.txt" in entries
        assert "file2.txt" in entries

    def test_root_includes_raw(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.sys_readdir.return_value = []
        entries = fuse_ops.readdir("/")
        assert ".raw" in entries

    def test_os_metadata_filtered(self, fuse_ops: Any, mock_nexus_fs: MagicMock) -> None:
        mock_nexus_fs.sys_readdir.return_value = [
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
        mock_nexus_fs.sys_readdir.assert_not_called()


class TestResolveFileSize:
    """_resolve_file_size: tiered size estimation for getattr (Issue #1568)."""

    def _make_handler(
        self,
        mock_nexus_fs: MagicMock,
        mock_cache: MagicMock,
    ) -> Any:
        """Create a MetadataHandler with controlled dependencies."""
        from nexus.fuse.ops.metadata_handler import MetadataHandler

        ctx = MagicMock()
        ctx.nexus_fs = mock_nexus_fs
        ctx.cache = mock_cache
        ctx.context = None
        return MetadataHandler(ctx)

    def test_raw_view_uses_metadata_size(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Raw files use metadata size directly (no multiplier)."""
        handler = self._make_handler(mock_nexus_fs, mock_cache)
        metadata = MagicMock()
        metadata.size = 2048

        result = handler._resolve_file_size("/data.bin", metadata, None)

        assert result == 2048

    def test_raw_view_type_uses_original_behavior(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """/.raw/ paths (view_type='raw') use metadata directly like raw files."""
        handler = self._make_handler(mock_nexus_fs, mock_cache)
        metadata = MagicMock()
        metadata.size = 5000

        result = handler._resolve_file_size("/report.xlsx", metadata, "raw")

        assert result == 5000

    def test_virtual_view_tier1_parsed_cache_exact(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Tier 1: parsed cache returns exact size."""
        mock_cache.get_parsed_size.return_value = 4200
        handler = self._make_handler(mock_nexus_fs, mock_cache)

        result = handler._resolve_file_size("/report.xlsx", None, "md")

        assert result == 4200
        mock_cache.get_parsed_size.assert_called_once_with("/report.xlsx", "md")

    def test_virtual_view_tier2_raw_content_estimate(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Tier 2: raw content cache size * multiplier."""
        mock_cache.get_parsed_size.return_value = None
        mock_cache.get_content.return_value = b"x" * 1000
        handler = self._make_handler(mock_nexus_fs, mock_cache)

        result = handler._resolve_file_size("/report.xlsx", None, "md")

        assert result == 1000 * _PARSED_SIZE_MULTIPLIER

    def test_virtual_view_tier2_skips_zero_byte(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Tier 2: 0-byte raw content falls through to next tier."""
        mock_cache.get_parsed_size.return_value = None
        mock_cache.get_content.return_value = b""
        metadata = MagicMock()
        metadata.size = 500
        handler = self._make_handler(mock_nexus_fs, mock_cache)

        result = handler._resolve_file_size("/empty.xlsx", metadata, "md")

        # Should fall through to tier 3 (metadata * multiplier)
        assert result == 500 * _PARSED_SIZE_MULTIPLIER

    def test_virtual_view_tier3_metadata_estimate(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Tier 3: metadata.size * multiplier."""
        mock_cache.get_parsed_size.return_value = None
        mock_cache.get_content.return_value = None
        handler = self._make_handler(mock_nexus_fs, mock_cache)
        metadata = MagicMock()
        metadata.size = 2000

        result = handler._resolve_file_size("/report.xlsx", metadata, "md")

        assert result == 2000 * _PARSED_SIZE_MULTIPLIER

    def test_virtual_view_tier4_stat_estimate(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Tier 4: stat() RPC size * multiplier."""
        mock_cache.get_parsed_size.return_value = None
        mock_cache.get_content.return_value = None
        mock_nexus_fs.stat.return_value = {"st_size": 3000}
        handler = self._make_handler(mock_nexus_fs, mock_cache)

        result = handler._resolve_file_size("/report.xlsx", None, "md")

        assert result == 3000 * _PARSED_SIZE_MULTIPLIER

    def test_virtual_view_tier5_default_constant(
        self, mock_nexus_fs: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Tier 5: 10 MiB default when nothing is available."""
        mock_cache.get_parsed_size.return_value = None
        mock_cache.get_content.return_value = None
        mock_nexus_fs.stat.return_value = None
        handler = self._make_handler(mock_nexus_fs, mock_cache)

        result = handler._resolve_file_size("/report.xlsx", None, "md")

        assert result == _VIRTUAL_VIEW_DEFAULT_SIZE

    @patch("nexus.fuse.ops.metadata_handler.stat_size_fallback", return_value=0)
    def test_virtual_view_no_full_read_triggered(
        self,
        _mock_stat: MagicMock,
        mock_nexus_fs: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """nexus_fs.sys_read() must NEVER be called during _resolve_file_size."""
        mock_cache.get_parsed_size.return_value = None
        mock_cache.get_content.return_value = None
        handler = self._make_handler(mock_nexus_fs, mock_cache)

        handler._resolve_file_size("/report.xlsx", None, "md")

        mock_nexus_fs.sys_read.assert_not_called()


class TestCacheGetParsedSize:
    """FUSECacheManager.get_parsed_size: lightweight size lookup."""

    def test_returns_none_when_not_cached(self) -> None:
        cache = FUSECacheManager()
        assert cache.get_parsed_size("/file.xlsx", "md") is None

    def test_returns_exact_size_when_cached(self) -> None:
        cache = FUSECacheManager()
        content = b"# Parsed markdown output\nSome data here."
        cache.cache_parsed("/file.xlsx", "md", content)

        assert cache.get_parsed_size("/file.xlsx", "md") == len(content)

    def test_different_view_types_independent(self) -> None:
        cache = FUSECacheManager()
        cache.cache_parsed("/file.xlsx", "md", b"short")
        cache.cache_parsed("/file.xlsx", "txt", b"a longer text output")

        assert cache.get_parsed_size("/file.xlsx", "md") == 5
        assert cache.get_parsed_size("/file.xlsx", "txt") == 20


class TestContextAwareMetadataHelpers:
    """Metadata helpers preserve the mount operation context."""

    def test_get_metadata_passes_context_to_sys_stat(self) -> None:
        ctx = MagicMock()
        ctx.context = object()
        ctx.nexus_fs.sys_stat.return_value = {"path": "/file.txt", "size": 9}

        result = asyncio.run(get_metadata(ctx, "/file.txt"))

        assert result.size == 9
        ctx.nexus_fs.sys_stat.assert_called_once_with("/file.txt", context=ctx.context)

    def test_stat_size_fallback_passes_context_to_stat(self) -> None:
        ctx = MagicMock()
        ctx.context = object()
        ctx.nexus_fs.stat.return_value = {"st_size": 123}

        result = stat_size_fallback(ctx, "/file.txt")

        assert result == 123
        ctx.nexus_fs.stat.assert_called_once_with("/file.txt", context=ctx.context)

    def test_returns_none_after_invalidation(self) -> None:
        cache = FUSECacheManager()
        cache.cache_parsed("/file.xlsx", "md", b"cached data")
        assert cache.get_parsed_size("/file.xlsx", "md") is not None

        cache.invalidate_path("/file.xlsx")
        assert cache.get_parsed_size("/file.xlsx", "md") is None
