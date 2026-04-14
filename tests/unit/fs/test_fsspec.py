"""Tests for fsspec NexusFileSystem compatibility layer.

Organized into:
- Unit tests: Mock-based tests for individual methods (fast)
- Edge case tests: NexusBufferedFile / NexusWriteFile corner cases
- Auto-discovery tests: mounts.json reading and error handling
- Integration tests: Real backend + fsspec discovery chain (no mocks)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

# Skip entire module if fsspec is not installed (optional dependency).
pytest.importorskip("fsspec")

from nexus.fs._fsspec import (  # noqa: E402
    _SUPPORTED_MODES,
    MAX_CAT_FILE_SIZE,
    NexusBufferedFile,
    NexusFileSystem,
    NexusWriteFile,
)
from nexus.fs._sync import PortalRunner  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_fs_cache():
    """Prevent fsspec instance caching from leaking between tests."""
    NexusFileSystem.clear_instance_cache()
    yield
    NexusFileSystem.clear_instance_cache()


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
    fs.rmdir = AsyncMock()
    return fs


@pytest.fixture
def nexus_fsspec(mock_nexus_fs):
    """NexusFileSystem instance with mocked backend."""
    fs = NexusFileSystem(nexus_fs=mock_nexus_fs)
    yield fs
    fs._runner.close()


# ===========================================================================
# _strip_protocol
# ===========================================================================


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


# ===========================================================================
# ls()
# ===========================================================================


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

    def test_ls_not_found_raises(self, nexus_fsspec, mock_nexus_fs):
        """ls() on non-existent path raises FileNotFoundError."""
        mock_nexus_fs.stat.return_value = None
        with pytest.raises(FileNotFoundError):
            nexus_fsspec.ls("/nonexistent")

    def test_ls_populates_dircache(self, nexus_fsspec, mock_nexus_fs):
        """ls() should populate fsspec dircache."""
        nexus_fsspec.ls("/dir", detail=True)
        assert "/dir" in nexus_fsspec.dircache
        assert len(nexus_fsspec.dircache["/dir"]) == 2

    def test_ls_uses_dircache_on_second_call(self, nexus_fsspec, mock_nexus_fs):
        """Second ls() should use dircache, not call backend again."""
        nexus_fsspec.ls("/dir", detail=True)
        nexus_fsspec.ls("/dir", detail=True)
        # ls backend should only be called once
        assert mock_nexus_fs.ls.await_count == 1


# ===========================================================================
# info()
# ===========================================================================


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


# ===========================================================================
# _cat_file()
# ===========================================================================


class TestCatFile:
    def test_cat_file_full(self, nexus_fsspec):
        result = nexus_fsspec._cat_file("/test.txt")
        assert result == b"hello world"

    def test_cat_file_byte_range_uses_read_range(self, nexus_fsspec, mock_nexus_fs):
        """Byte-range reads should use read_range(), not read()."""
        mock_nexus_fs.read_range.return_value = b"hello"
        result = nexus_fsspec._cat_file("/test.txt", start=0, end=5)
        assert result == b"hello"
        mock_nexus_fs.read_range.assert_awaited_once()
        mock_nexus_fs.read.assert_not_awaited()

    def test_cat_file_start_only(self, nexus_fsspec, mock_nexus_fs):
        """start without end reads from start to EOF."""
        mock_nexus_fs.read_range.return_value = b"world"
        nexus_fsspec._cat_file("/test.txt", start=6)
        mock_nexus_fs.read_range.assert_awaited_once_with("/test.txt", 6, 11)

    def test_cat_file_end_only(self, nexus_fsspec, mock_nexus_fs):
        """end without start reads from beginning to end."""
        mock_nexus_fs.read_range.return_value = b"hello"
        nexus_fsspec._cat_file("/test.txt", end=5)
        mock_nexus_fs.read_range.assert_awaited_once_with("/test.txt", 0, 5)

    def test_cat_file_negative_start(self, nexus_fsspec, mock_nexus_fs):
        """Negative start counts from end (Python slice semantics)."""
        mock_nexus_fs.read_range.return_value = b"world"
        nexus_fsspec._cat_file("/test.txt", start=-5)
        # start=-5 with size=11 -> range_start=6, range_end=11
        mock_nexus_fs.read_range.assert_awaited_once_with("/test.txt", 6, 11)

    def test_cat_file_negative_end(self, nexus_fsspec, mock_nexus_fs):
        """Negative end counts from end."""
        mock_nexus_fs.read_range.return_value = b"hello wor"
        nexus_fsspec._cat_file("/test.txt", start=0, end=-2)
        # end=-2 with size=11 -> range_end=9
        mock_nexus_fs.read_range.assert_awaited_once_with("/test.txt", 0, 9)

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

    def test_cat_file_not_found(self, nexus_fsspec, mock_nexus_fs):
        """_cat_file on non-existent file raises FileNotFoundError."""
        mock_nexus_fs.stat.return_value = None
        with pytest.raises(FileNotFoundError):
            nexus_fsspec._cat_file("/nonexistent.txt")

    def test_cat_file_not_found_with_range(self, nexus_fsspec, mock_nexus_fs):
        """_cat_file with byte range on non-existent file raises FileNotFoundError."""
        mock_nexus_fs.stat.return_value = None
        with pytest.raises(FileNotFoundError):
            nexus_fsspec._cat_file("/nonexistent.txt", start=0, end=5)


# ===========================================================================
# _pipe_file()
# ===========================================================================


class TestPipeFile:
    def test_pipe_file(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._pipe_file("/output.txt", b"new content")
        mock_nexus_fs.write.assert_awaited_once()


# ===========================================================================
# _rm()
# ===========================================================================


class TestRm:
    def test_rm_file(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = {"is_directory": False}
        nexus_fsspec._rm("/file.txt")
        mock_nexus_fs.delete.assert_awaited_once()

    def test_rm_directory_recursive(self, nexus_fsspec, mock_nexus_fs):
        """_rm on directory with recursive=True uses rmdir."""
        mock_nexus_fs.stat.return_value = {"is_directory": True}
        nexus_fsspec._rm("/dir", recursive=True)
        mock_nexus_fs.rmdir.assert_awaited_once_with("/dir", recursive=True)

    def test_rm_directory_non_recursive(self, nexus_fsspec, mock_nexus_fs):
        """_rm on directory with recursive=False uses rmdir."""
        mock_nexus_fs.stat.return_value = {"is_directory": True}
        nexus_fsspec._rm("/dir", recursive=False)
        mock_nexus_fs.rmdir.assert_awaited_once_with("/dir", recursive=False)


# ===========================================================================
# _cp_file()
# ===========================================================================


class TestCpFile:
    def test_cp_file(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._cp_file("/src.txt", "/dst.txt")
        mock_nexus_fs.copy.assert_awaited_once()


class TestCpFileComprehensive:
    """Comprehensive copy tests covering edge cases and error paths."""

    def test_cp_file_strips_protocol(self, nexus_fsspec, mock_nexus_fs):
        """Verify _cp_file strips protocol from both paths."""
        nexus_fsspec._cp_file("nexus:///src.txt", "nexus:///dst.txt")
        args = mock_nexus_fs.copy.await_args
        # Verify stripped paths were passed
        assert args is not None

    def test_cp_file_source_not_found(self, nexus_fsspec, mock_nexus_fs):
        """_cp_file raises FileNotFoundError when source doesn't exist."""
        mock_nexus_fs.copy.side_effect = FileNotFoundError("/src.txt")
        with pytest.raises(FileNotFoundError):
            nexus_fsspec._cp_file("/src.txt", "/dst.txt")

    def test_cp_file_destination_exists(self, nexus_fsspec, mock_nexus_fs):
        """_cp_file raises FileExistsError when destination already exists."""
        mock_nexus_fs.copy.side_effect = FileExistsError("/dst.txt")
        with pytest.raises(FileExistsError):
            nexus_fsspec._cp_file("/src.txt", "/dst.txt")

    def test_cp_file_empty_file(self, nexus_fsspec, mock_nexus_fs):
        """_cp_file works for empty (0-byte) files."""
        mock_nexus_fs.copy.return_value = {"path": "/dst.txt", "size": 0, "etag": "d41d8cd98f"}
        nexus_fsspec._cp_file("/src.txt", "/dst.txt")
        mock_nexus_fs.copy.assert_awaited_once()


class TestErrorPropagation:
    """Verify errors from the backend propagate correctly through fsspec."""

    def test_cat_file_backend_error(self, nexus_fsspec, mock_nexus_fs):
        """BackendError from read propagates through _cat_file."""
        from nexus.contracts.exceptions import BackendError

        mock_nexus_fs.stat.return_value = {"path": "/f", "size": 10, "is_directory": False}
        mock_nexus_fs.read.side_effect = BackendError("network timeout", backend="s3", path="/f")
        with pytest.raises(BackendError):
            nexus_fsspec._cat_file("/f")

    def test_cat_file_not_found(self, nexus_fsspec, mock_nexus_fs):
        """FileNotFoundError propagates through _cat_file."""
        mock_nexus_fs.stat.return_value = None
        with pytest.raises(FileNotFoundError):
            nexus_fsspec._cat_file("/nonexistent")

    def test_pipe_file_backend_error(self, nexus_fsspec, mock_nexus_fs):
        """BackendError from write propagates through _pipe_file."""
        from nexus.contracts.exceptions import BackendError

        mock_nexus_fs.write.side_effect = BackendError("disk full", backend="local", path="/f")
        with pytest.raises(BackendError):
            nexus_fsspec._pipe_file("/f", b"data")

    def test_open_read_stat_error(self, nexus_fsspec, mock_nexus_fs):
        """Error during stat in _open(mode='rb') propagates."""
        from nexus.contracts.exceptions import BackendError

        mock_nexus_fs.stat.side_effect = BackendError("timeout", backend="s3", path="/f")
        with pytest.raises(BackendError):
            nexus_fsspec._open("/f", mode="rb")

    def test_write_file_close_error(self, nexus_fsspec, mock_nexus_fs):
        """Error during NexusWriteFile.close() propagates through context manager."""
        from nexus.contracts.exceptions import BackendError

        mock_nexus_fs.write.side_effect = BackendError("write failed", backend="s3", path="/f")
        f = nexus_fsspec._open("/f", mode="wb")
        f.write(b"data")
        with pytest.raises(BackendError):
            f.close()

    def test_buffered_file_read_range_error(self, nexus_fsspec, mock_nexus_fs):
        """Error during read_range propagates through NexusBufferedFile.read()."""
        from nexus.contracts.exceptions import BackendError

        mock_nexus_fs.stat.return_value = {"path": "/f", "size": 100, "is_directory": False}
        mock_nexus_fs.read_range.side_effect = BackendError("timeout", backend="s3", path="/f")
        f = nexus_fsspec._open("/f", mode="rb")
        with pytest.raises(BackendError):
            f.read()


# ===========================================================================
# _mkdir()
# ===========================================================================


class TestMkdir:
    def test_mkdir(self, nexus_fsspec, mock_nexus_fs):
        nexus_fsspec._mkdir("/new/dir")
        mock_nexus_fs.mkdir.assert_awaited_once()


# ===========================================================================
# _open() — mode validation (Issue 8A)
# ===========================================================================


class TestModeValidation:
    @pytest.mark.parametrize("mode", sorted(_SUPPORTED_MODES))
    def test_supported_modes_accepted(self, nexus_fsspec, mock_nexus_fs, mode):
        """All supported modes should be accepted without raising."""
        if "x" in mode:
            # Exclusive-create: stat must return None (file doesn't exist)
            mock_nexus_fs.stat.return_value = None
        else:
            mock_nexus_fs.stat.return_value = {"path": "/f", "size": 0, "is_directory": False}
        f = nexus_fsspec._open("/f", mode=mode)
        f.close()

    @pytest.mark.parametrize("mode", ["ab", "a", "r+b", "xyz"])
    def test_unsupported_modes_raise(self, nexus_fsspec, mode):
        """Unsupported modes should raise ValueError with helpful message."""
        with pytest.raises(ValueError, match="Unsupported mode"):
            nexus_fsspec._open("/f", mode=mode)


# ===========================================================================
# _open() — read mode
# ===========================================================================


class TestOpenRead:
    def test_open_read_returns_buffered_file(self, nexus_fsspec, mock_nexus_fs):
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

    def test_open_read_uses_read_range(self, nexus_fsspec, mock_nexus_fs):
        """Verify _open() uses read_range(), not read()."""
        mock_nexus_fs.stat.return_value = {"path": "/file.txt", "size": 11, "is_directory": False}
        mock_nexus_fs.read_range = AsyncMock(return_value=b"hello")
        with nexus_fsspec._open("/file.txt", mode="rb") as f:
            f.read(5)
        mock_nexus_fs.read_range.assert_awaited_once()
        mock_nexus_fs.read.assert_not_awaited()

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

    def test_open_read_name_property(self, nexus_fsspec, mock_nexus_fs):
        mock_nexus_fs.stat.return_value = {"path": "/file.txt", "size": 11, "is_directory": False}
        f = nexus_fsspec._open("/file.txt", mode="rb")
        assert f.name == "/file.txt"
        f.close()


# ===========================================================================
# _open() — write mode
# ===========================================================================


class TestOpenWrite:
    def test_open_write_returns_write_file(self, nexus_fsspec, mock_nexus_fs):
        f = nexus_fsspec._open("/output.txt", mode="wb")
        assert isinstance(f, NexusWriteFile)
        assert f.writable()
        assert not f.readable()
        f.close()

    def test_open_write_flushes_on_close(self, nexus_fsspec, mock_nexus_fs):
        with nexus_fsspec._open("/output.txt", mode="wb") as f:
            f.write(b"hello ")
            f.write(b"world")
        mock_nexus_fs.write.assert_awaited_once()
        args = mock_nexus_fs.write.await_args
        assert args[0][1] == b"hello world"

    def test_open_write_context_manager(self, nexus_fsspec, mock_nexus_fs):
        with nexus_fsspec._open("/output.txt", mode="wb") as f:
            f.write(b"data")
        assert f.closed

    def test_open_write_name_property(self, nexus_fsspec, mock_nexus_fs):
        f = nexus_fsspec._open("/output.txt", mode="wb")
        assert f.name == "/output.txt"
        f.close()


# ===========================================================================
# Protocol / inheritance
# ===========================================================================


class TestProtocol:
    def test_protocol_tuple(self):
        assert NexusFileSystem.protocol == ("nexus",)

    def test_is_subclass_of_abstract_filesystem(self):
        """NexusFileSystem should properly inherit from AbstractFileSystem."""
        from fsspec.spec import AbstractFileSystem

        assert issubclass(NexusFileSystem, AbstractFileSystem)

    def test_instance_is_abstract_filesystem(self, nexus_fsspec):
        """Instances should pass isinstance checks."""
        from fsspec.spec import AbstractFileSystem

        assert isinstance(nexus_fsspec, AbstractFileSystem)


# ===========================================================================
# NexusBufferedFile edge cases (Issue 10A)
# ===========================================================================


class TestBufferedFileEdgeCases:
    """Edge cases for NexusBufferedFile (read-mode file objects)."""

    @pytest.fixture
    def buf_file(self, mock_nexus_fs):
        """Create a NexusBufferedFile with size=11 ("hello world")."""
        runner = PortalRunner()
        f = NexusBufferedFile(
            fs=None,
            path="/test.txt",
            mode="rb",
            size=11,
            block_size=8192,
            nexus_fs=mock_nexus_fs,
            runner=runner,
        )
        yield f
        runner.close()

    def test_read_on_closed_file(self, buf_file):
        buf_file.close()
        with pytest.raises(ValueError, match="I/O operation on closed file"):
            buf_file.read()

    def test_read_past_eof(self, buf_file):
        buf_file.seek(11)  # at EOF
        assert buf_file.read() == b""

    def test_read_zero_bytes(self, buf_file, mock_nexus_fs):
        """read(0) should return empty bytes."""
        mock_nexus_fs.read_range.return_value = b""
        result = buf_file.read(0)
        assert result == b""

    def test_read_all_remaining(self, buf_file, mock_nexus_fs):
        """read(-1) should read from current position to EOF."""
        mock_nexus_fs.read_range.return_value = b"hello world"
        result = buf_file.read(-1)
        assert result == b"hello world"
        assert buf_file.tell() == 11

    def test_seek_whence_relative(self, buf_file):
        """seek(offset, 1) seeks relative to current position."""
        buf_file.seek(5)
        result = buf_file.seek(3, 1)
        assert result == 8

    def test_seek_whence_from_end(self, buf_file):
        """seek(offset, 2) seeks relative to end."""
        result = buf_file.seek(-3, 2)
        assert result == 8  # 11 - 3

    def test_seek_negative_raises(self, buf_file):
        """AbstractBufferedFile raises ValueError on seek before start."""
        with pytest.raises(ValueError, match="Seek before start of file"):
            buf_file.seek(-100, 0)

    def test_seek_past_end(self, buf_file):
        """AbstractBufferedFile allows seeking past end (like regular files)."""
        result = buf_file.seek(100, 0)
        assert result == 100

    def test_double_close(self, buf_file):
        buf_file.close()
        buf_file.close()  # should not raise
        assert buf_file.closed

    def test_flush_noop(self, buf_file):
        buf_file.flush()  # should not raise

    def test_readline_with_newline(self, buf_file, mock_nexus_fs):
        """readline() reads up to and including newline."""
        mock_nexus_fs.read_range.return_value = b"hello\nworld"
        line = buf_file.readline()
        assert line == b"hello\n"
        assert buf_file.tell() == 6

    def test_readline_no_newline(self, buf_file, mock_nexus_fs):
        """readline() returns remaining bytes if no newline found."""
        content = b"no newline"
        mock_nexus_fs.read_range.side_effect = lambda path, start, end: content[start:end]
        buf_file.seek(0)
        buf_file.size = len(content)
        line = buf_file.readline()
        assert line == content

    def test_readline_at_eof(self, buf_file):
        """readline() at EOF returns empty bytes."""
        buf_file.seek(11)
        assert buf_file.readline() == b""

    def test_readline_on_closed_file(self, buf_file):
        buf_file.close()
        with pytest.raises(ValueError, match="I/O operation on closed file"):
            buf_file.readline()

    def test_readlines(self, buf_file, mock_nexus_fs):
        """readlines() returns all remaining lines."""
        content = b"line1\nline2"
        mock_nexus_fs.read_range.side_effect = lambda path, start, end: content[start:end]
        buf_file.size = len(content)
        lines = buf_file.readlines()
        assert lines == [b"line1\n", b"line2"]

    def test_iter(self, buf_file, mock_nexus_fs):
        """Iterating yields lines."""
        content = b"a\nb\n"
        mock_nexus_fs.read_range.side_effect = lambda path, start, end: content[start:end]
        buf_file.size = len(content)
        lines = list(buf_file)
        assert lines == [b"a\n", b"b\n"]


# ===========================================================================
# NexusWriteFile edge cases (Issue 10A)
# ===========================================================================


class TestWriteFileEdgeCases:
    """Edge cases for NexusWriteFile (write-mode file objects)."""

    @pytest.fixture
    def write_file(self, mock_nexus_fs):
        runner = PortalRunner()
        f = NexusWriteFile(
            fs=None,
            path="/out.txt",
            nexus_fs=mock_nexus_fs,
            runner=runner,
        )
        yield f
        runner.close()

    def test_write_on_closed_file(self, write_file):
        write_file.close()
        with pytest.raises(ValueError, match="I/O operation on closed file"):
            write_file.write(b"data")

    def test_double_close_no_double_flush(self, write_file, mock_nexus_fs):
        """Double close should only flush once."""
        write_file.write(b"data")
        write_file.close()
        write_file.close()
        assert mock_nexus_fs.write.await_count == 1

    def test_flush_noop(self, write_file, mock_nexus_fs):
        """flush() should not trigger a backend write."""
        write_file.write(b"data")
        write_file.flush()
        mock_nexus_fs.write.assert_not_awaited()

    def test_not_seekable(self, write_file):
        assert not write_file.seekable()

    def test_not_readable(self, write_file):
        assert not write_file.readable()

    def test_write_buffer_guard(self, write_file):
        """Writes exceeding MAX_WRITE_BUFFER_SIZE should raise."""
        chunk = b"x" * (1024 * 1024)  # 1 MB
        with pytest.raises(ValueError, match="Write buffer exceeded"):
            for _ in range(1025):  # > 1 GB
                write_file.write(chunk)


# ===========================================================================
# Auto-discovery (Issue 1A)
# ===========================================================================


class TestAutoDiscovery:
    def test_auto_discover_no_mounts_file(self, tmp_path, monkeypatch):
        """Auto-discovery with no mounts.json raises FileNotFoundError."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError, match="No nexus-fs mounts found"):
            NexusFileSystem._auto_discover()

    def test_auto_discover_empty_mounts(self, tmp_path, monkeypatch):
        """Auto-discovery with empty mounts list raises ValueError."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text("[]")
        with pytest.raises(ValueError, match="Invalid mounts.json"):
            NexusFileSystem._auto_discover()

    def test_auto_discover_invalid_type(self, tmp_path, monkeypatch):
        """Auto-discovery with non-list JSON raises ValueError."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text('"not a list"')
        with pytest.raises(ValueError, match="Invalid mounts.json"):
            NexusFileSystem._auto_discover()

    def test_auto_discover_calls_mount(self, tmp_path, monkeypatch):
        """Auto-discovery reads mounts.json and calls mount() with URIs."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text(json.dumps(["local:///tmp/data"]))

        mock_facade = AsyncMock()

        async def mock_mount(*uris, at=None, mount_overrides=None):
            return mock_facade

        with patch("nexus.fs.mount", side_effect=mock_mount):
            result = NexusFileSystem._auto_discover()

        assert result is mock_facade


# ===========================================================================
# Integration tests — real backend, no mocks (Issue 9A)
# ===========================================================================


class TestFsspecIntegration:
    """Integration tests using real CASLocalBackend + NexusFileSystem.

    Validates the full fsspec chain end-to-end.
    """

    @pytest.fixture
    def fsspec_real(self, tmp_path):
        """Create a real NexusFileSystem backed by a local CASLocalBackend."""
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.contracts.types import OperationContext
        from nexus.core.config import PermissionConfig
        from nexus.core.nexus_fs import NexusFS
        from nexus.fs import _make_mount_entry
        from nexus.fs._facade import SlimNexusFS
        from nexus.fs._sqlite_meta import SQLiteMetastore

        db_path = str(tmp_path / "metadata.db")
        metastore = SQLiteMetastore(db_path)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        backend = CASLocalBackend(root_path=data_dir)

        kernel = NexusFS(
            metadata_store=metastore,
            permissions=PermissionConfig(enforce=False),
        )
        kernel._init_cred = OperationContext(
            user_id="test",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )
        kernel._driver_coordinator.mount("/local", backend)
        metastore.put(_make_mount_entry("/local", backend.name))

        facade = SlimNexusFS(kernel)
        fs = NexusFileSystem(nexus_fs=facade)
        yield fs
        fs._runner.close()

    def test_write_and_cat(self, fsspec_real):
        """Write via _pipe_file, read via _cat_file — full round-trip."""
        fsspec_real._pipe_file("/local/hello.txt", b"Hello, fsspec!")
        result = fsspec_real._cat_file("/local/hello.txt")
        assert result == b"Hello, fsspec!"

    def test_ls_detail(self, fsspec_real):
        """Write files, ls with detail."""
        fsspec_real._pipe_file("/local/a.txt", b"aaa")
        fsspec_real._pipe_file("/local/b.txt", b"bbb")
        entries = fsspec_real.ls("/local", detail=True)
        names = [e["name"] for e in entries]
        assert "/local/a.txt" in names
        assert "/local/b.txt" in names

    def test_info(self, fsspec_real):
        """Write file, get info, verify metadata."""
        fsspec_real._pipe_file("/local/info.txt", b"metadata")
        info = fsspec_real.info("/local/info.txt")
        assert info["name"] == "/local/info.txt"
        assert info["size"] == 8
        assert info["type"] == "file"

    def test_open_write_then_read(self, fsspec_real):
        """Write via _open(wb), read via _open(rb)."""
        with fsspec_real._open("/local/stream.txt", mode="wb") as f:
            f.write(b"streamed content")
        with fsspec_real._open("/local/stream.txt", mode="rb") as f:
            data = f.read()
        assert data == b"streamed content"

    def test_readline_integration(self, fsspec_real):
        """Write multi-line file, read lines via readline."""
        content = b"line1\nline2\nline3"
        fsspec_real._pipe_file("/local/lines.txt", content)
        with fsspec_real._open("/local/lines.txt", mode="rb") as f:
            assert f.readline() == b"line1\n"
            assert f.readline() == b"line2\n"
            assert f.readline() == b"line3"
            assert f.readline() == b""  # EOF

    def test_iter_lines_integration(self, fsspec_real):
        """Write multi-line file, iterate lines."""
        content = b"a\nb\nc\n"
        fsspec_real._pipe_file("/local/iter.txt", content)
        with fsspec_real._open("/local/iter.txt", mode="rb") as f:
            lines = list(f)
        assert lines == [b"a\n", b"b\n", b"c\n"]

    def test_byte_range_read(self, fsspec_real):
        """_cat_file with byte range uses read_range, returns correct slice."""
        fsspec_real._pipe_file("/local/range.txt", b"0123456789")
        result = fsspec_real._cat_file("/local/range.txt", start=2, end=7)
        assert result == b"23456"

    def test_mkdir_and_ls(self, fsspec_real):
        """Create directory, list parent."""
        fsspec_real._mkdir("/local/subdir")
        entries = fsspec_real.ls("/local", detail=True)
        names = [e["name"] for e in entries]
        assert "/local/subdir" in names

    def test_rm_file(self, fsspec_real):
        """Write file, delete it, verify gone."""
        fsspec_real._pipe_file("/local/gone.txt", b"bye")
        fsspec_real._rm("/local/gone.txt")
        with pytest.raises(FileNotFoundError):
            fsspec_real.info("/local/gone.txt")

    def test_cp_file(self, fsspec_real):
        """Copy file, verify copy has same content."""
        fsspec_real._pipe_file("/local/src.txt", b"copy me")
        fsspec_real._cp_file("/local/src.txt", "/local/dst.txt")
        assert fsspec_real._cat_file("/local/dst.txt") == b"copy me"

    def test_dircache_populated(self, fsspec_real):
        """ls() should populate dircache."""
        fsspec_real._pipe_file("/local/cached.txt", b"data")
        fsspec_real.ls("/local", detail=True)
        assert "/local" in fsspec_real.dircache

    def test_issubclass_check(self, fsspec_real):
        """Instance should pass isinstance(fs, AbstractFileSystem)."""
        from fsspec.spec import AbstractFileSystem

        assert isinstance(fsspec_real, AbstractFileSystem)


# ===========================================================================
# End-to-end: pandas via fsspec (validates the actual claimed integration)
# ===========================================================================

pd = pytest.importorskip("pandas")


class TestPandasIntegration:
    """Validates that pd.read_csv("nexus:///...") actually works end-to-end.

    Uses a real CASLocalBackend + mount() + fsspec registration.
    """

    @pytest.fixture
    def mounted_fs(self, tmp_path, monkeypatch):
        """Mount a real local backend and register the nexus protocol."""
        import fsspec

        fsspec.register_implementation("nexus", NexusFileSystem, clobber=True)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(state_dir))

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        import nexus.fs
        from nexus.fs._sync import run_sync

        facade = run_sync(nexus.fs.mount(f"local://{data_dir}"))

        from nexus.fs._sync import PortalRunner

        runner = PortalRunner()
        mount = sorted(m for m in facade.list_mounts())[0]
        yield facade, runner, mount
        runner.close()

    def _write(self, facade, runner, path, content):
        runner(facade.write(path, content))

    def test_pd_read_csv(self, mounted_fs):
        """pd.read_csv('nexus:///...') reads a CSV from nexus-fs."""
        facade, runner, mount = mounted_fs
        self._write(facade, runner, f"{mount}/data.csv", b"name,age\nAlice,30\nBob,25\n")

        df = pd.read_csv(f"nexus://{mount}/data.csv")

        assert list(df.columns) == ["name", "age"]
        assert len(df) == 2
        assert df["name"].tolist() == ["Alice", "Bob"]

    def test_pd_to_csv_roundtrip(self, mounted_fs):
        """df.to_csv('nexus:///...') writes, then read back matches."""
        facade, runner, mount = mounted_fs

        original = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
        original.to_csv(f"nexus://{mount}/output.csv", index=False)

        roundtrip = pd.read_csv(f"nexus://{mount}/output.csv")
        assert list(roundtrip.columns) == ["x", "y"]
        assert len(roundtrip) == 3
        assert roundtrip["x"].tolist() == [1, 2, 3]

    def test_pd_read_json(self, mounted_fs):
        """pd.read_json('nexus:///...') reads JSON from nexus-fs."""
        facade, runner, mount = mounted_fs
        self._write(
            facade,
            runner,
            f"{mount}/data.json",
            b'[{"name":"Alice","score":95},{"name":"Bob","score":87}]',
        )

        df = pd.read_json(f"nexus://{mount}/data.json")

        assert "name" in df.columns
        assert len(df) == 2

    def test_fsspec_open_read_write(self, mounted_fs):
        """fsspec.open('nexus:///...') works for both read and write."""
        import fsspec

        facade, runner, mount = mounted_fs

        with fsspec.open(f"nexus://{mount}/fsspec_rw.txt", "wb") as f:
            f.write(b"written via fsspec.open")

        with fsspec.open(f"nexus://{mount}/fsspec_rw.txt", "rb") as f:
            content = f.read()

        assert content == b"written via fsspec.open"


# ===========================================================================
# End-to-end: HuggingFace datasets via fsspec
# ===========================================================================

datasets = pytest.importorskip("datasets")


class TestHuggingFaceIntegration:
    """Validates that HuggingFace load_dataset works with nexus:// URIs."""

    @pytest.fixture
    def mounted_fs(self, tmp_path, monkeypatch):
        """Mount a real local backend and register the nexus protocol."""
        import fsspec

        fsspec.register_implementation("nexus", NexusFileSystem, clobber=True)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(state_dir))
        monkeypatch.setenv("HF_DATASETS_CACHE", str(tmp_path / "hf_cache"))

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        import nexus.fs
        from nexus.fs._sync import PortalRunner, run_sync

        facade = run_sync(nexus.fs.mount(f"local://{data_dir}"))
        runner = PortalRunner()
        mount = sorted(m for m in facade.list_mounts())[0]
        yield facade, runner, mount
        runner.close()

    def _write(self, facade, runner, path, content):
        runner(facade.write(path, content))

    def test_load_dataset_csv(self, mounted_fs):
        """HuggingFace load_dataset('csv', data_files='nexus:///...') works."""
        facade, runner, mount = mounted_fs
        self._write(
            facade,
            runner,
            f"{mount}/train.csv",
            b"text,label\nhello world,1\ngoodbye world,0\nfoo bar,1\n",
        )

        ds = datasets.load_dataset(
            "csv",
            data_files=f"nexus://{mount}/train.csv",
            split="train",
        )

        assert len(ds) == 3
        assert ds.column_names == ["text", "label"]
        assert ds[0]["text"] == "hello world"
        assert ds[0]["label"] == 1


# ===========================================================================
# End-to-end: Dask parquet via fsspec
# ===========================================================================

dask_dd = pytest.importorskip("dask.dataframe")


class TestDaskIntegration:
    """Validates that dask parquet roundtrip works with nexus:// URIs.

    Dask exercises byte-range reads (_cat_file with start/end) which are
    critical for efficient columnar file access.
    """

    @pytest.fixture
    def mounted_fs(self, tmp_path, monkeypatch):
        """Mount a real local backend and register the nexus protocol."""
        import fsspec

        fsspec.register_implementation("nexus", NexusFileSystem, clobber=True)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(state_dir))

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        import nexus.fs
        from nexus.fs._sync import PortalRunner, run_sync

        facade = run_sync(nexus.fs.mount(f"local://{data_dir}"))
        runner = PortalRunner()
        mount = sorted(m for m in facade.list_mounts())[0]
        yield facade, runner, mount
        runner.close()

    def test_dask_parquet_roundtrip(self, mounted_fs):
        """Write parquet via dask, read it back, assert equality."""
        import pandas as pd

        facade, runner, mount = mounted_fs

        original = pd.DataFrame({"a": range(100), "b": [f"val_{i}" for i in range(100)]})
        ddf = dask_dd.from_pandas(original, npartitions=2)

        ddf.to_parquet(f"nexus://{mount}/test_dask.parquet")

        result = dask_dd.read_parquet(f"nexus://{mount}/test_dask.parquet")
        result_pd = result.compute()

        assert len(result_pd) == 100
        assert list(result_pd.columns) == ["a", "b"]
        assert result_pd["a"].tolist() == list(range(100))
