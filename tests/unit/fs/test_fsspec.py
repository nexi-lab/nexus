"""Tests for fsspec NexusFileSystem compatibility layer.

Verifies that NexusFileSystem implements the fsspec contract:
- ls() returns correct format (detail=True/False)
- info() returns required keys (name, size, type)
- _cat_file() reads content with byte ranges
- _cat_file() enforces size guard (1 GB limit)
- _pipe_file() writes content
- _rm() deletes files
- _cp_file() copies files
- _mkdir() creates directories
- _open() returns file-like objects (read + write modes)
- _strip_protocol() correctly removes nexus:// prefix
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.fs._fsspec import (
    MAX_CAT_FILE_SIZE,
    NexusBufferedFile,
    NexusFileSystem,
    NexusWriteFile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nexus_fs():
    """Mock SlimNexusFS facade with async methods."""
    fs = AsyncMock()
    fs.read = AsyncMock(return_value=b"hello world")
    fs.read_range = AsyncMock(return_value=b"hello world")
    fs.write = AsyncMock(return_value={"path": "/test.txt", "size": 11, "etag": "abc"})
    fs.ls = AsyncMock(
        return_value=[
            {"path": "/dir/file1.txt", "size": 100, "entry_type": 0},
            {"path": "/dir/subdir", "size": 4096, "entry_type": 1},
        ]
    )
    fs.stat = AsyncMock(
        return_value={
            "path": "/test.txt",
            "size": 11,
            "etag": "abc123",
            "is_directory": False,
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
        }
    )
    fs.delete = AsyncMock()
    fs.copy = AsyncMock(return_value={"path": "/dst.txt", "size": 11})
    fs.mkdir = AsyncMock()
    return fs


@pytest.fixture
def sync_caller():
    """Sync caller that runs coroutines immediately."""
    import asyncio

    def _call(coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # Use a simple approach: create a new loop if needed
    def _sync(coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    return _sync


@pytest.fixture
def nexus_fsspec(mock_nexus_fs, sync_caller):
    """NexusFileSystem instance with mocked backend."""
    fs = NexusFileSystem.__new__(NexusFileSystem)
    fs._nexus = mock_nexus_fs
    fs._sync = sync_caller
    return fs


# ---------------------------------------------------------------------------
# _strip_protocol
# ---------------------------------------------------------------------------


class TestStripProtocol:
    @pytest.mark.parametrize(
        "input_path,expected",
        [
            ("nexus:///s3/bucket/file.txt", "/s3/bucket/file.txt"),
            ("nexus://s3/bucket/file.txt", "/s3/bucket/file.txt"),
            ("nexus:/s3/bucket/file.txt", "/s3/bucket/file.txt"),
            ("/s3/bucket/file.txt", "/s3/bucket/file.txt"),
            ("s3/bucket/file.txt", "/s3/bucket/file.txt"),
            ("nexus:///", "/"),
            ("nexus://", "/"),
        ],
    )
    def test_strip_protocol(self, input_path, expected):
        assert NexusFileSystem._strip_protocol(input_path) == expected


# ---------------------------------------------------------------------------
# ls()
# ---------------------------------------------------------------------------


class TestLs:
    def test_ls_detail(self, nexus_fsspec, mock_nexus_fs):
        result = nexus_fsspec.ls("/dir", detail=True)
        assert len(result) == 2
        assert result[0]["name"] == "/dir/file1.txt"
        assert result[0]["type"] == "file"
        assert result[0]["size"] == 100
        assert result[1]["name"] == "/dir/subdir"
        assert result[1]["type"] == "directory"

    def test_ls_no_detail(self, nexus_fsspec, mock_nexus_fs):
        result = nexus_fsspec.ls("/dir", detail=False)
        assert result == ["/dir/file1.txt", "/dir/subdir"]


# ---------------------------------------------------------------------------
# info()
# ---------------------------------------------------------------------------


class TestInfo:
    def test_info_file(self, nexus_fsspec):
        result = nexus_fsspec.info("/test.txt")
        assert result["name"] == "/test.txt"
        assert result["size"] == 11
        assert result["type"] == "file"
        assert "etag" in result

    def test_info_not_found(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = None
        with pytest.raises(FileNotFoundError):
            nexus_fsspec.info("/nonexistent.txt")


# ---------------------------------------------------------------------------
# _cat_file()
# ---------------------------------------------------------------------------


class TestCatFile:
    def test_cat_file_full(self, nexus_fsspec):
        result = nexus_fsspec._cat_file("/test.txt")
        assert result == b"hello world"

    def test_cat_file_byte_range(self, nexus_fsspec):
        result = nexus_fsspec._cat_file("/test.txt", start=0, end=5)
        assert result == b"hello"

    def test_cat_file_size_guard(self, nexus_fsspec, mock_nexus_fs):
        """Files larger than MAX_CAT_FILE_SIZE should be refused."""
        mock_nexus_fs.stat.return_value = {
            "path": "/huge.bin",
            "size": MAX_CAT_FILE_SIZE + 1,
            "is_directory": False,
        }
        with pytest.raises(ValueError, match="too large"):
            nexus_fsspec._cat_file("/huge.bin")

    def test_cat_file_at_limit(self, nexus_fsspec, mock_nexus_fs):
        """Files exactly at the limit should succeed."""
        mock_nexus_fs.stat.return_value = {
            "path": "/exact.bin",
            "size": MAX_CAT_FILE_SIZE,
            "is_directory": False,
        }
        result = nexus_fsspec._cat_file("/exact.bin")
        assert result == b"hello world"


# ---------------------------------------------------------------------------
# _pipe_file()
# ---------------------------------------------------------------------------


class TestPipeFile:
    def test_pipe_file(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._pipe_file("/output.txt", b"new content")
        mock_nexus_fs.write.assert_called_once()


# ---------------------------------------------------------------------------
# _rm()
# ---------------------------------------------------------------------------


class TestRm:
    def test_rm(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._rm("/file.txt")
        mock_nexus_fs.delete.assert_called_once()


# ---------------------------------------------------------------------------
# _cp_file()
# ---------------------------------------------------------------------------


class TestCpFile:
    def test_cp_file(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._cp_file("/src.txt", "/dst.txt")
        mock_nexus_fs.copy.assert_called_once()


# ---------------------------------------------------------------------------
# _mkdir()
# ---------------------------------------------------------------------------


class TestMkdir:
    def test_mkdir(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._mkdir("/new/dir")
        mock_nexus_fs.mkdir.assert_called_once()


# ---------------------------------------------------------------------------
# _open() — read mode
# ---------------------------------------------------------------------------


class TestOpenRead:
    def test_open_read_returns_file_like(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = {"path": "/file.txt", "size": 11, "is_directory": False}
        f = nexus_fsspec._open("/file.txt", mode="rb")
        assert isinstance(f, NexusBufferedFile)
        assert f.readable()
        assert not f.writable()
        assert f.seekable()
        f.close()

    def test_open_read_content(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = {"path": "/file.txt", "size": 11, "is_directory": False}
        with nexus_fsspec._open("/file.txt", mode="rb") as f:
            data = f.read()
            assert data == b"hello world"

    def test_open_read_uses_read_range_not_full_read(self, nexus_fsspec, mock_nexus_fs):
        """Verify _open() uses read_range() for streaming, not read() which loads full file."""
        mock_nexus_fs.stat.return_value = {"path": "/file.txt", "size": 11, "is_directory": False}
        mock_nexus_fs.read_range = AsyncMock(return_value=b"hello")
        with nexus_fsspec._open("/file.txt", mode="rb") as f:
            f.read(5)
        mock_nexus_fs.read_range.assert_called_once()
        # read() should NOT be called — only read_range()
        mock_nexus_fs.read.assert_not_called()

    def test_open_read_seek_tell(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = {"path": "/file.txt", "size": 11, "is_directory": False}
        f = nexus_fsspec._open("/file.txt", mode="rb")
        assert f.tell() == 0
        f.seek(5)
        assert f.tell() == 5
        f.close()

    def test_open_read_not_found(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = None
        with pytest.raises(FileNotFoundError):
            nexus_fsspec._open("/nonexistent.txt", mode="rb")


# ---------------------------------------------------------------------------
# _open() — write mode
# ---------------------------------------------------------------------------


class TestOpenWrite:
    def test_open_write_returns_file_like(self, nexus_fsspec, mock_nexus_fs):
        f = nexus_fsspec._open("/output.txt", mode="wb")
        assert isinstance(f, NexusWriteFile)
        assert f.writable()
        assert not f.readable()
        f.close()

    def test_open_write_flushes_on_close(self, nexus_fsspec, mock_nexus_fs):
        with nexus_fsspec._open("/output.txt", mode="wb") as f:
            f.write(b"hello ")
            f.write(b"world")
        # Should have written the combined content on close
        mock_nexus_fs.write.assert_called_once()
        args = mock_nexus_fs.write.call_args
        assert args[0][1] == b"hello world"

    def test_open_write_context_manager(self, nexus_fsspec, mock_nexus_fs):
        with nexus_fsspec._open("/output.txt", mode="wb") as f:
            f.write(b"data")
        assert f.closed


# ---------------------------------------------------------------------------
# Protocol attribute
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_protocol_tuple(self):
        assert NexusFileSystem.protocol == ("nexus",)
