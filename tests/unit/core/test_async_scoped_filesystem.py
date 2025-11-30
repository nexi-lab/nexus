"""Tests for AsyncScopedFilesystem wrapper.

Tests path scoping/unscoping for multi-tenant isolation with async client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from nexus.core.async_scoped_filesystem import AsyncScopedFilesystem


@pytest.fixture
def mock_async_fs() -> MagicMock:
    """Create a mock async filesystem."""
    fs = MagicMock()
    # Set up property mocks
    type(fs).agent_id = PropertyMock(return_value="test-agent")
    type(fs).tenant_id = PropertyMock(return_value="test-tenant")
    return fs


@pytest.fixture
def scoped_fs(mock_async_fs: MagicMock) -> AsyncScopedFilesystem:
    """Create an AsyncScopedFilesystem with a test root."""
    return AsyncScopedFilesystem(mock_async_fs, root="/tenants/team_12/users/user_1")


class TestPathScoping:
    """Test path scoping and unscoping logic."""

    def test_scope_path_basic(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test basic path scoping."""
        assert scoped_fs._scope_path("/workspace/file.txt") == (
            "/tenants/team_12/users/user_1/workspace/file.txt"
        )

    def test_scope_path_root(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test scoping root path."""
        assert scoped_fs._scope_path("/") == "/tenants/team_12/users/user_1/"

    def test_unscope_path_basic(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test basic path unscoping."""
        assert (
            scoped_fs._unscope_path("/tenants/team_12/users/user_1/workspace/file.txt")
            == "/workspace/file.txt"
        )

    def test_unscope_path_root(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test unscoping to root."""
        assert scoped_fs._unscope_path("/tenants/team_12/users/user_1") == "/"


class TestRootNormalization:
    """Test root path normalization."""

    def test_trailing_slash_removed(self, mock_async_fs: MagicMock) -> None:
        """Test that trailing slash is removed from root."""
        fs = AsyncScopedFilesystem(mock_async_fs, root="/tenants/team_12/")
        assert fs.root == "/tenants/team_12"

    def test_empty_root(self, mock_async_fs: MagicMock) -> None:
        """Test empty root (no scoping)."""
        fs = AsyncScopedFilesystem(mock_async_fs, root="")
        assert fs.root == ""
        assert fs._scope_path("/workspace/file.txt") == "/workspace/file.txt"


class TestProperties:
    """Test property delegation."""

    def test_agent_id(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test agent_id property delegation."""
        assert scoped_fs.agent_id == "test-agent"

    def test_tenant_id(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test tenant_id property delegation."""
        assert scoped_fs.tenant_id == "test-tenant"

    def test_root_property(self, scoped_fs: AsyncScopedFilesystem) -> None:
        """Test root property."""
        assert scoped_fs.root == "/tenants/team_12/users/user_1"


class TestCoreFileOperations:
    """Test core file operation path scoping (async)."""

    @pytest.mark.asyncio
    async def test_read(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test read with path scoping."""
        mock_async_fs.read = AsyncMock(return_value=b"content")
        result = await scoped_fs.read("/workspace/file.txt")
        mock_async_fs.read.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", None, False
        )
        assert result == b"content"

    @pytest.mark.asyncio
    async def test_read_with_metadata(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test read with metadata unscopes path."""
        mock_async_fs.read = AsyncMock(
            return_value={
                "content": b"data",
                "path": "/tenants/team_12/users/user_1/workspace/file.txt",
                "etag": "abc",
            }
        )
        result = await scoped_fs.read("/workspace/file.txt", return_metadata=True)
        assert result["path"] == "/workspace/file.txt"

    @pytest.mark.asyncio
    async def test_read_bulk(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test read_bulk with path scoping."""
        mock_async_fs.read_bulk = AsyncMock(
            return_value={
                "/tenants/team_12/users/user_1/workspace/a.txt": b"content_a",
                "/tenants/team_12/users/user_1/workspace/b.txt": b"content_b",
            }
        )
        result = await scoped_fs.read_bulk(["/workspace/a.txt", "/workspace/b.txt"])
        assert "/workspace/a.txt" in result
        assert "/workspace/b.txt" in result
        assert result["/workspace/a.txt"] == b"content_a"

    @pytest.mark.asyncio
    async def test_write(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test write with path scoping."""
        mock_async_fs.write = AsyncMock(
            return_value={
                "path": "/tenants/team_12/users/user_1/workspace/file.txt",
                "etag": "abc",
            }
        )
        result = await scoped_fs.write("/workspace/file.txt", b"content")
        mock_async_fs.write.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt",
            b"content",
            None,
            None,
            False,
            False,
        )
        assert result["path"] == "/workspace/file.txt"

    @pytest.mark.asyncio
    async def test_write_batch(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test write_batch with path scoping."""
        mock_async_fs.write_batch = AsyncMock(
            return_value=[
                {"path": "/tenants/team_12/users/user_1/workspace/a.txt"},
                {"path": "/tenants/team_12/users/user_1/workspace/b.txt"},
            ]
        )
        files = [("/workspace/a.txt", b"a"), ("/workspace/b.txt", b"b")]
        result = await scoped_fs.write_batch(files)
        assert result[0]["path"] == "/workspace/a.txt"
        assert result[1]["path"] == "/workspace/b.txt"

    @pytest.mark.asyncio
    async def test_delete(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test delete with path scoping."""
        mock_async_fs.delete = AsyncMock(return_value=True)
        await scoped_fs.delete("/workspace/file.txt")
        mock_async_fs.delete.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", None
        )

    @pytest.mark.asyncio
    async def test_rename(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test rename with path scoping for both paths."""
        mock_async_fs.rename = AsyncMock(
            return_value={"path": "/tenants/team_12/users/user_1/workspace/new.txt"}
        )
        await scoped_fs.rename("/workspace/old.txt", "/workspace/new.txt")
        mock_async_fs.rename.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/old.txt",
            "/tenants/team_12/users/user_1/workspace/new.txt",
            None,
        )

    @pytest.mark.asyncio
    async def test_exists(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test exists with path scoping."""
        mock_async_fs.exists = AsyncMock(return_value=True)
        result = await scoped_fs.exists("/workspace/file.txt")
        mock_async_fs.exists.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", None
        )
        assert result is True


class TestFileDiscoveryOperations:
    """Test file discovery operation path scoping (async)."""

    @pytest.mark.asyncio
    async def test_list_paths_only(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test list returns unscoped paths."""
        mock_async_fs.list = AsyncMock(
            return_value=[
                "/tenants/team_12/users/user_1/workspace/a.txt",
                "/tenants/team_12/users/user_1/workspace/b.txt",
            ]
        )
        result = await scoped_fs.list("/workspace")
        assert result == ["/workspace/a.txt", "/workspace/b.txt"]

    @pytest.mark.asyncio
    async def test_list_with_details(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test list with details unscopes paths."""
        mock_async_fs.list = AsyncMock(
            return_value=[
                {"path": "/tenants/team_12/users/user_1/workspace/a.txt", "size": 100},
            ]
        )
        result = await scoped_fs.list("/workspace", details=True)
        assert result[0]["path"] == "/workspace/a.txt"

    @pytest.mark.asyncio
    async def test_glob(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test glob returns unscoped paths."""
        mock_async_fs.glob = AsyncMock(
            return_value=[
                "/tenants/team_12/users/user_1/workspace/test_a.py",
                "/tenants/team_12/users/user_1/workspace/test_b.py",
            ]
        )
        result = await scoped_fs.glob("test_*.py", "/workspace")
        mock_async_fs.glob.assert_called_once_with(
            "test_*.py", "/tenants/team_12/users/user_1/workspace", None
        )
        assert result == ["/workspace/test_a.py", "/workspace/test_b.py"]

    @pytest.mark.asyncio
    async def test_grep(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test grep returns unscoped file paths."""
        mock_async_fs.grep = AsyncMock(
            return_value=[
                {
                    "file": "/tenants/team_12/users/user_1/workspace/app.py",
                    "line": 10,
                    "content": "TODO: fix",
                }
            ]
        )
        result = await scoped_fs.grep("TODO", "/workspace")
        assert result[0]["file"] == "/workspace/app.py"


class TestDirectoryOperations:
    """Test directory operation path scoping (async)."""

    @pytest.mark.asyncio
    async def test_mkdir(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test mkdir with path scoping."""
        mock_async_fs.mkdir = AsyncMock(
            return_value={"path": "/tenants/team_12/users/user_1/workspace/new_dir"}
        )
        await scoped_fs.mkdir("/workspace/new_dir", parents=True, exist_ok=True)
        mock_async_fs.mkdir.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/new_dir", True, True, None
        )

    @pytest.mark.asyncio
    async def test_rmdir(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test rmdir with path scoping."""
        mock_async_fs.rmdir = AsyncMock()
        await scoped_fs.rmdir("/workspace/old_dir", recursive=True)
        mock_async_fs.rmdir.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/old_dir", True, None
        )

    @pytest.mark.asyncio
    async def test_is_directory(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test is_directory with path scoping."""
        mock_async_fs.is_directory = AsyncMock(return_value=True)
        result = await scoped_fs.is_directory("/workspace/dir")
        mock_async_fs.is_directory.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/dir", None
        )
        assert result is True


class TestVersionOperations:
    """Test version operation path scoping (async)."""

    @pytest.mark.asyncio
    async def test_get_version(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test get_version with path scoping."""
        mock_async_fs.get_version = AsyncMock(return_value=b"old content")
        result = await scoped_fs.get_version("/workspace/file.txt", 1)
        mock_async_fs.get_version.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", 1, None
        )
        assert result == b"old content"

    @pytest.mark.asyncio
    async def test_list_versions(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test list_versions with path scoping."""
        mock_async_fs.list_versions = AsyncMock(
            return_value=[
                {"version": 1, "path": "/tenants/team_12/users/user_1/workspace/file.txt"}
            ]
        )
        result = await scoped_fs.list_versions("/workspace/file.txt")
        assert result[0]["path"] == "/workspace/file.txt"

    @pytest.mark.asyncio
    async def test_rollback(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test rollback with path scoping."""
        mock_async_fs.rollback = AsyncMock()
        await scoped_fs.rollback("/workspace/file.txt", 1)
        mock_async_fs.rollback.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", 1, None
        )


class TestMountOperations:
    """Test mount operation path scoping (async)."""

    @pytest.mark.asyncio
    async def test_add_mount(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test add_mount with path scoping."""
        mock_async_fs.add_mount = AsyncMock(return_value="mount-123")
        result = await scoped_fs.add_mount("/external/gcs", "gcs", {"bucket": "my-bucket"})
        mock_async_fs.add_mount.assert_called_once_with(
            "/tenants/team_12/users/user_1/external/gcs",
            "gcs",
            {"bucket": "my-bucket"},
            0,
            False,
            None,
        )
        assert result == "mount-123"

    @pytest.mark.asyncio
    async def test_list_mounts(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test list_mounts unscopes paths."""
        mock_async_fs.list_mounts = AsyncMock(
            return_value=[{"mount_point": "/tenants/team_12/users/user_1/external/gcs"}]
        )
        result = await scoped_fs.list_mounts()
        assert result[0]["mount_point"] == "/external/gcs"


class TestMemoryOperations:
    """Test memory operation path scoping (async)."""

    @pytest.mark.asyncio
    async def test_register_memory(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test register_memory with path scoping."""
        mock_async_fs.register_memory = AsyncMock(
            return_value={"path": "/tenants/team_12/users/user_1/workspace/memory"}
        )
        result = await scoped_fs.register_memory("/workspace/memory", name="test-memory")
        mock_async_fs.register_memory.assert_called_once()
        assert result["path"] == "/workspace/memory"

    @pytest.mark.asyncio
    async def test_list_registered_memories(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test list_registered_memories unscopes paths."""
        mock_async_fs.list_registered_memories = AsyncMock(
            return_value=[{"path": "/tenants/team_12/users/user_1/workspace/memory"}]
        )
        result = await scoped_fs.list_registered_memories()
        assert result[0]["path"] == "/workspace/memory"


class TestAgentOperations:
    """Test that agent operations are passed through without path scoping."""

    @pytest.mark.asyncio
    async def test_register_agent(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test register_agent is passed through."""
        mock_async_fs.register_agent = AsyncMock(return_value={"agent_id": "agent-123"})
        result = await scoped_fs.register_agent("agent-123", "Test Agent")
        mock_async_fs.register_agent.assert_called_once_with("agent-123", "Test Agent", None, False)
        assert result["agent_id"] == "agent-123"


class TestLifecycleManagement:
    """Test lifecycle management (async)."""

    @pytest.mark.asyncio
    async def test_close(self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock) -> None:
        """Test close is delegated."""
        mock_async_fs.close = AsyncMock()
        await scoped_fs.close()
        mock_async_fs.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager(
        self, scoped_fs: AsyncScopedFilesystem, mock_async_fs: MagicMock
    ) -> None:
        """Test async context manager."""
        mock_async_fs.close = AsyncMock()
        async with scoped_fs as fs:
            assert fs is scoped_fs
        mock_async_fs.close.assert_called_once()
