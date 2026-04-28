"""Tests for ScopedFilesystem wrapper.

Tests path scoping/unscoping for multi-zone isolation.
"""

from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.bricks.filesystem.scoped_filesystem import ScopedFilesystem


@pytest.fixture
def mock_fs() -> MagicMock:
    """Create a mock filesystem with async syscall support."""
    fs = MagicMock()
    # Set up property mocks
    type(fs).agent_id = PropertyMock(return_value="test-agent")
    type(fs).zone_id = PropertyMock(return_value="test-zone")
    # NexusFS syscalls and Tier 2 methods are sync def — use MagicMock
    fs.sys_read = MagicMock()
    fs.sys_write = MagicMock()
    fs.sys_stat = MagicMock()
    fs.sys_setattr = MagicMock()
    fs.sys_unlink = MagicMock()
    fs.sys_rename = MagicMock()
    fs.mkdir = MagicMock()
    fs.rmdir = MagicMock()
    fs.sys_readdir = MagicMock()
    fs.access = MagicMock()
    fs.is_directory = MagicMock()
    fs.read = MagicMock()
    fs.write = MagicMock()
    fs.append = MagicMock()
    fs.write_batch = MagicMock()
    return fs


@pytest.fixture
def scoped_fs(mock_fs: MagicMock) -> ScopedFilesystem:
    """Create a ScopedFilesystem with a test root."""
    return ScopedFilesystem(mock_fs, root="/zones/team_12/users/user_1")


class TestPathScoping:
    """Test path scoping and unscoping logic."""

    def test_scope_path_basic(self, scoped_fs: ScopedFilesystem) -> None:
        """Test basic path scoping."""
        assert scoped_fs._scope_path("/workspace/file.txt") == (
            "/zones/team_12/users/user_1/workspace/file.txt"
        )

    def test_scope_path_root(self, scoped_fs: ScopedFilesystem) -> None:
        """Test scoping root path."""
        assert scoped_fs._scope_path("/") == "/zones/team_12/users/user_1/"

    def test_scope_path_without_leading_slash(self, scoped_fs: ScopedFilesystem) -> None:
        """Test scoping path without leading slash."""
        assert scoped_fs._scope_path("workspace/file.txt") == (
            "/zones/team_12/users/user_1/workspace/file.txt"
        )

    def test_unscope_path_basic(self, scoped_fs: ScopedFilesystem) -> None:
        """Test basic path unscoping."""
        assert (
            scoped_fs._unscope_path("/zones/team_12/users/user_1/workspace/file.txt")
            == "/workspace/file.txt"
        )

    def test_unscope_path_root(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping to root."""
        assert scoped_fs._unscope_path("/zones/team_12/users/user_1") == "/"

    def test_unscope_path_not_scoped(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping path that doesn't have root prefix."""
        assert scoped_fs._unscope_path("/other/path") == "/other/path"

    def test_unscope_paths_list(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping a list of paths."""
        paths = [
            "/zones/team_12/users/user_1/workspace/a.txt",
            "/zones/team_12/users/user_1/shared/b.txt",
        ]
        assert scoped_fs._unscope_paths(paths) == ["/workspace/a.txt", "/shared/b.txt"]

    def test_unscope_dict(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping paths in a dict."""
        d = {
            "path": "/zones/team_12/users/user_1/workspace/file.txt",
            "size": 100,
            "content_id": "abc123",
        }
        result = scoped_fs._unscope_dict(d, ["path"])
        assert result["path"] == "/workspace/file.txt"
        assert result["size"] == 100
        assert result["content_id"] == "abc123"


class TestRootNormalization:
    """Test root path normalization."""

    def test_trailing_slash_removed(self, mock_fs: MagicMock) -> None:
        """Test that trailing slash is removed from root."""
        fs = ScopedFilesystem(mock_fs, root="/zones/team_12/")
        assert fs.root == "/zones/team_12"

    def test_leading_slash_added(self, mock_fs: MagicMock) -> None:
        """Test that leading slash is added if missing."""
        fs = ScopedFilesystem(mock_fs, root="zones/team_12")
        assert fs.root == "/zones/team_12"

    def test_empty_root(self, mock_fs: MagicMock) -> None:
        """Test empty root (no scoping)."""
        fs = ScopedFilesystem(mock_fs, root="")
        assert fs.root == ""
        assert fs._scope_path("/workspace/file.txt") == "/workspace/file.txt"

    def test_root_with_only_slash(self, mock_fs: MagicMock) -> None:
        """Test root with only slash."""
        fs = ScopedFilesystem(mock_fs, root="/")
        assert fs.root == ""
        assert fs._scope_path("/workspace/file.txt") == "/workspace/file.txt"


class TestProperties:
    """Test property delegation."""

    def test_agent_id(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test agent_id property delegation."""
        assert scoped_fs.agent_id == "test-agent"

    def test_zone_id(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test zone_id property delegation."""
        assert scoped_fs.zone_id == "test-zone"

    def test_root_property(self, scoped_fs: ScopedFilesystem) -> None:
        """Test root property."""
        assert scoped_fs.root == "/zones/team_12/users/user_1"

    def test_wrapped_fs_property(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test wrapped_fs property."""
        assert scoped_fs.wrapped_fs is mock_fs


class TestCoreFileOperations:
    """Test core file operation path scoping."""

    @pytest.mark.asyncio
    async def test_read(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test sys_read with path scoping (POSIX pread returns bytes)."""
        mock_fs.sys_read.return_value = b"content"
        result = scoped_fs.sys_read("/workspace/file.txt")
        mock_fs.sys_read.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/file.txt",
            count=None,
            offset=0,
            context=None,
        )
        assert result == b"content"

    @pytest.mark.asyncio
    async def test_read_with_metadata(
        self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock
    ) -> None:
        """Test read() with metadata unscopes path (Tier 2 convenience)."""
        mock_fs.read.return_value = {
            "content": b"data",
            "path": "/zones/team_12/users/user_1/workspace/file.txt",
            "etag": "abc",
        }
        result = scoped_fs.read("/workspace/file.txt", return_metadata=True)
        assert result["path"] == "/workspace/file.txt"

    @pytest.mark.asyncio
    async def test_write(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test sys_write with path scoping (returns dict with bytes_written + created)."""
        mock_fs.sys_write.return_value = {
            "path": "/zones/team_12/users/user_1/workspace/file.txt",
            "bytes_written": 7,
            "created": True,
        }
        result = scoped_fs.sys_write("/workspace/file.txt", b"content")
        mock_fs.sys_write.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/file.txt",
            b"content",
            count=None,
            offset=0,
            context=None,
        )
        assert result["bytes_written"] == 7

    @pytest.mark.asyncio
    async def test_write_batch(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test write_batch with path scoping."""
        mock_fs.write_batch.return_value = [
            {"path": "/zones/team_12/users/user_1/workspace/a.txt"},
            {"path": "/zones/team_12/users/user_1/workspace/b.txt"},
        ]
        files = [("/workspace/a.txt", b"a"), ("/workspace/b.txt", b"b")]
        result = scoped_fs.write_batch(files)
        mock_fs.write_batch.assert_called_once()
        call_args = mock_fs.write_batch.call_args[0][0]
        assert call_args[0][0] == "/zones/team_12/users/user_1/workspace/a.txt"
        assert result[0]["path"] == "/workspace/a.txt"

    @pytest.mark.asyncio
    async def test_append(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test append with path scoping."""
        mock_fs.append.return_value = {"path": "/zones/team_12/users/user_1/workspace/log.txt"}
        scoped_fs.append("/workspace/log.txt", b"log entry")
        mock_fs.append.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/log.txt",
            b"log entry",
            context=None,
        )

    @pytest.mark.asyncio
    async def test_delete(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test delete with path scoping."""
        scoped_fs.sys_unlink("/workspace/file.txt")
        mock_fs.sys_unlink.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/file.txt", context=None
        )

    @pytest.mark.asyncio
    async def test_rename(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test rename with path scoping for both paths."""
        scoped_fs.sys_rename("/workspace/old.txt", "/workspace/new.txt")
        mock_fs.sys_rename.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/old.txt",
            "/zones/team_12/users/user_1/workspace/new.txt",
            context=None,
        )

    @pytest.mark.asyncio
    async def test_exists(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test exists with path scoping."""
        mock_fs.access.return_value = True
        result = scoped_fs.access("/workspace/file.txt")
        mock_fs.access.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/file.txt", context=None
        )
        assert result is True


class TestFileDiscoveryOperations:
    """Test file discovery operation path scoping."""

    @pytest.mark.asyncio
    async def test_list_paths_only(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list returns unscoped paths."""
        mock_fs.sys_readdir.return_value = [
            "/zones/team_12/users/user_1/workspace/a.txt",
            "/zones/team_12/users/user_1/workspace/b.txt",
        ]
        result = scoped_fs.sys_readdir("/workspace")
        assert result == ["/workspace/a.txt", "/workspace/b.txt"]

    @pytest.mark.asyncio
    async def test_list_with_details(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list with details unscopes paths."""
        mock_fs.sys_readdir.return_value = [
            {"path": "/zones/team_12/users/user_1/workspace/a.txt", "size": 100},
            {"path": "/zones/team_12/users/user_1/workspace/b.txt", "size": 200},
        ]
        result = scoped_fs.sys_readdir("/workspace", details=True)
        assert result[0]["path"] == "/workspace/a.txt"
        assert result[1]["path"] == "/workspace/b.txt"

    def test_glob(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test glob returns unscoped paths."""
        mock_fs.service("search").glob.return_value = [
            "/zones/team_12/users/user_1/workspace/test_a.py",
            "/zones/team_12/users/user_1/workspace/test_b.py",
        ]
        result = scoped_fs.glob("test_*.py", "/workspace")
        mock_fs.service("search").glob.assert_called_once_with(
            "test_*.py", "/zones/team_12/users/user_1/workspace", None
        )
        assert result == ["/workspace/test_a.py", "/workspace/test_b.py"]

    def test_grep(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test grep returns unscoped file paths."""
        mock_fs.service("search").grep.return_value = [
            {
                "file": "/zones/team_12/users/user_1/workspace/app.py",
                "line": 10,
                "content": "TODO: fix",
            }
        ]
        result = scoped_fs.grep("TODO", "/workspace")
        mock_fs.service("search").grep.assert_called_once()
        assert result[0]["file"] == "/workspace/app.py"


class TestDirectoryOperations:
    """Test directory operation path scoping."""

    @pytest.mark.asyncio
    async def test_mkdir(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test mkdir with path scoping."""
        scoped_fs.mkdir("/workspace/new_dir", parents=True, exist_ok=True)
        mock_fs.mkdir.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/new_dir", True, True, context=None
        )

    @pytest.mark.asyncio
    async def test_rmdir(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test rmdir with path scoping."""
        scoped_fs.rmdir("/workspace/old_dir", recursive=True)
        mock_fs.rmdir.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/old_dir", recursive=True, context=None
        )

    @pytest.mark.asyncio
    async def test_is_directory(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test is_directory with path scoping."""
        mock_fs.is_directory.return_value = True
        result = scoped_fs.is_directory("/workspace/dir")
        mock_fs.is_directory.assert_called_once_with(
            "/zones/team_12/users/user_1/workspace/dir", context=None
        )
        assert result is True


class TestLifecycleManagement:
    """Test lifecycle management."""

    def test_close(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test close is delegated."""
        scoped_fs.close()
        mock_fs.close.assert_called_once()

    def test_context_manager(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test context manager."""
        with scoped_fs as fs:
            assert fs is scoped_fs
        mock_fs.close.assert_called_once()
