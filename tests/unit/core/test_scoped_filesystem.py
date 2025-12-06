"""Tests for ScopedFilesystem wrapper.

Tests path scoping/unscoping for multi-tenant isolation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.core.scoped_filesystem import ScopedFilesystem


@pytest.fixture
def mock_fs() -> MagicMock:
    """Create a mock filesystem."""
    fs = MagicMock()
    # Set up property mocks
    type(fs).agent_id = PropertyMock(return_value="test-agent")
    type(fs).tenant_id = PropertyMock(return_value="test-tenant")
    return fs


@pytest.fixture
def scoped_fs(mock_fs: MagicMock) -> ScopedFilesystem:
    """Create a ScopedFilesystem with a test root."""
    return ScopedFilesystem(mock_fs, root="/tenants/team_12/users/user_1")


class TestPathScoping:
    """Test path scoping and unscoping logic."""

    def test_scope_path_basic(self, scoped_fs: ScopedFilesystem) -> None:
        """Test basic path scoping."""
        assert scoped_fs._scope_path("/workspace/file.txt") == (
            "/tenants/team_12/users/user_1/workspace/file.txt"
        )

    def test_scope_path_root(self, scoped_fs: ScopedFilesystem) -> None:
        """Test scoping root path."""
        assert scoped_fs._scope_path("/") == "/tenants/team_12/users/user_1/"

    def test_scope_path_without_leading_slash(self, scoped_fs: ScopedFilesystem) -> None:
        """Test scoping path without leading slash."""
        assert scoped_fs._scope_path("workspace/file.txt") == (
            "/tenants/team_12/users/user_1/workspace/file.txt"
        )

    def test_unscope_path_basic(self, scoped_fs: ScopedFilesystem) -> None:
        """Test basic path unscoping."""
        assert (
            scoped_fs._unscope_path("/tenants/team_12/users/user_1/workspace/file.txt")
            == "/workspace/file.txt"
        )

    def test_unscope_path_root(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping to root."""
        assert scoped_fs._unscope_path("/tenants/team_12/users/user_1") == "/"

    def test_unscope_path_not_scoped(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping path that doesn't have root prefix."""
        assert scoped_fs._unscope_path("/other/path") == "/other/path"

    def test_unscope_paths_list(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping a list of paths."""
        paths = [
            "/tenants/team_12/users/user_1/workspace/a.txt",
            "/tenants/team_12/users/user_1/shared/b.txt",
        ]
        assert scoped_fs._unscope_paths(paths) == ["/workspace/a.txt", "/shared/b.txt"]

    def test_unscope_dict(self, scoped_fs: ScopedFilesystem) -> None:
        """Test unscoping paths in a dict."""
        d = {
            "path": "/tenants/team_12/users/user_1/workspace/file.txt",
            "size": 100,
            "etag": "abc123",
        }
        result = scoped_fs._unscope_dict(d, ["path"])
        assert result["path"] == "/workspace/file.txt"
        assert result["size"] == 100
        assert result["etag"] == "abc123"


class TestRootNormalization:
    """Test root path normalization."""

    def test_trailing_slash_removed(self, mock_fs: MagicMock) -> None:
        """Test that trailing slash is removed from root."""
        fs = ScopedFilesystem(mock_fs, root="/tenants/team_12/")
        assert fs.root == "/tenants/team_12"

    def test_leading_slash_added(self, mock_fs: MagicMock) -> None:
        """Test that leading slash is added if missing."""
        fs = ScopedFilesystem(mock_fs, root="tenants/team_12")
        assert fs.root == "/tenants/team_12"

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

    def test_tenant_id(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test tenant_id property delegation."""
        assert scoped_fs.tenant_id == "test-tenant"

    def test_root_property(self, scoped_fs: ScopedFilesystem) -> None:
        """Test root property."""
        assert scoped_fs.root == "/tenants/team_12/users/user_1"

    def test_wrapped_fs_property(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test wrapped_fs property."""
        assert scoped_fs.wrapped_fs is mock_fs


class TestCoreFileOperations:
    """Test core file operation path scoping."""

    def test_read(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test read with path scoping."""
        mock_fs.read.return_value = b"content"
        result = scoped_fs.read("/workspace/file.txt")
        mock_fs.read.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", None, False
        )
        assert result == b"content"

    def test_read_with_metadata(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test read with metadata unscopes path."""
        mock_fs.read.return_value = {
            "content": b"data",
            "path": "/tenants/team_12/users/user_1/workspace/file.txt",
            "etag": "abc",
        }
        result = scoped_fs.read("/workspace/file.txt", return_metadata=True)
        assert result["path"] == "/workspace/file.txt"

    def test_write(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test write with path scoping."""
        mock_fs.write.return_value = {
            "path": "/tenants/team_12/users/user_1/workspace/file.txt",
            "etag": "abc",
        }
        result = scoped_fs.write("/workspace/file.txt", b"content")
        mock_fs.write.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt",
            b"content",
            None,
            None,
            False,
            False,
        )
        assert result["path"] == "/workspace/file.txt"

    def test_write_batch(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test write_batch with path scoping."""
        mock_fs.write_batch.return_value = [
            {"path": "/tenants/team_12/users/user_1/workspace/a.txt"},
            {"path": "/tenants/team_12/users/user_1/workspace/b.txt"},
        ]
        files = [("/workspace/a.txt", b"a"), ("/workspace/b.txt", b"b")]
        result = scoped_fs.write_batch(files)
        mock_fs.write_batch.assert_called_once()
        call_args = mock_fs.write_batch.call_args[0][0]
        assert call_args[0][0] == "/tenants/team_12/users/user_1/workspace/a.txt"
        assert result[0]["path"] == "/workspace/a.txt"

    def test_append(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test append with path scoping."""
        mock_fs.append.return_value = {"path": "/tenants/team_12/users/user_1/workspace/log.txt"}
        scoped_fs.append("/workspace/log.txt", b"log entry")
        mock_fs.append.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/log.txt",
            b"log entry",
            None,
            None,
            False,
        )

    def test_delete(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test delete with path scoping."""
        scoped_fs.delete("/workspace/file.txt")
        mock_fs.delete.assert_called_once_with("/tenants/team_12/users/user_1/workspace/file.txt")

    def test_rename(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test rename with path scoping for both paths."""
        scoped_fs.rename("/workspace/old.txt", "/workspace/new.txt")
        mock_fs.rename.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/old.txt",
            "/tenants/team_12/users/user_1/workspace/new.txt",
        )

    def test_exists(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test exists with path scoping."""
        mock_fs.exists.return_value = True
        result = scoped_fs.exists("/workspace/file.txt")
        mock_fs.exists.assert_called_once_with("/tenants/team_12/users/user_1/workspace/file.txt")
        assert result is True


class TestFileDiscoveryOperations:
    """Test file discovery operation path scoping."""

    def test_list_paths_only(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list returns unscoped paths."""
        mock_fs.list.return_value = [
            "/tenants/team_12/users/user_1/workspace/a.txt",
            "/tenants/team_12/users/user_1/workspace/b.txt",
        ]
        result = scoped_fs.list("/workspace")
        assert result == ["/workspace/a.txt", "/workspace/b.txt"]

    def test_list_with_details(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list with details unscopes paths."""
        mock_fs.list.return_value = [
            {"path": "/tenants/team_12/users/user_1/workspace/a.txt", "size": 100},
            {"path": "/tenants/team_12/users/user_1/workspace/b.txt", "size": 200},
        ]
        result = scoped_fs.list("/workspace", details=True)
        assert result[0]["path"] == "/workspace/a.txt"
        assert result[1]["path"] == "/workspace/b.txt"

    def test_glob(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test glob returns unscoped paths."""
        mock_fs.glob.return_value = [
            "/tenants/team_12/users/user_1/workspace/test_a.py",
            "/tenants/team_12/users/user_1/workspace/test_b.py",
        ]
        result = scoped_fs.glob("test_*.py", "/workspace")
        mock_fs.glob.assert_called_once_with(
            "test_*.py", "/tenants/team_12/users/user_1/workspace", None
        )
        assert result == ["/workspace/test_a.py", "/workspace/test_b.py"]

    def test_grep(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test grep returns unscoped file paths."""
        mock_fs.grep.return_value = [
            {
                "file": "/tenants/team_12/users/user_1/workspace/app.py",
                "line": 10,
                "content": "TODO: fix",
            }
        ]
        result = scoped_fs.grep("TODO", "/workspace")
        mock_fs.grep.assert_called_once()
        assert result[0]["file"] == "/workspace/app.py"


class TestDirectoryOperations:
    """Test directory operation path scoping."""

    def test_mkdir(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test mkdir with path scoping."""
        scoped_fs.mkdir("/workspace/new_dir", parents=True, exist_ok=True)
        mock_fs.mkdir.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/new_dir", True, True
        )

    def test_rmdir(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test rmdir with path scoping."""
        scoped_fs.rmdir("/workspace/old_dir", recursive=True)
        mock_fs.rmdir.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/old_dir", True
        )

    def test_is_directory(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test is_directory with path scoping."""
        mock_fs.is_directory.return_value = True
        result = scoped_fs.is_directory("/workspace/dir")
        mock_fs.is_directory.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/dir", None
        )
        assert result is True


class TestVersionOperations:
    """Test version operation path scoping."""

    def test_get_version(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test get_version with path scoping."""
        mock_fs.get_version.return_value = b"old content"
        result = scoped_fs.get_version("/workspace/file.txt", 1)
        mock_fs.get_version.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", 1
        )
        assert result == b"old content"

    def test_list_versions(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list_versions with path scoping."""
        mock_fs.list_versions.return_value = [
            {"version": 1, "path": "/tenants/team_12/users/user_1/workspace/file.txt"}
        ]
        result = scoped_fs.list_versions("/workspace/file.txt")
        mock_fs.list_versions.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt"
        )
        assert result[0]["path"] == "/workspace/file.txt"

    def test_rollback(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test rollback with path scoping."""
        scoped_fs.rollback("/workspace/file.txt", 1)
        mock_fs.rollback.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace/file.txt", 1, None
        )


class TestWorkspaceOperations:
    """Test workspace operation path scoping."""

    def test_register_workspace(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test register_workspace with path scoping."""
        mock_fs.register_workspace.return_value = {
            "path": "/tenants/team_12/users/user_1/workspace"
        }
        result = scoped_fs.register_workspace("/workspace", name="my-workspace")
        mock_fs.register_workspace.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace",
            "my-workspace",
            None,
            None,
            None,
            None,
            None,
            None,
        )
        assert result["path"] == "/workspace"

    def test_list_workspaces(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list_workspaces unscopes paths."""
        mock_fs.list_workspaces.return_value = [
            {"path": "/tenants/team_12/users/user_1/workspace", "name": "ws1"}
        ]
        result = scoped_fs.list_workspaces()
        assert result[0]["path"] == "/workspace"

    def test_workspace_snapshot(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test workspace_snapshot with path scoping."""
        mock_fs.workspace_snapshot.return_value = {
            "workspace_path": "/tenants/team_12/users/user_1/workspace"
        }
        result = scoped_fs.workspace_snapshot(workspace_path="/workspace")
        mock_fs.workspace_snapshot.assert_called_once_with(
            "/tenants/team_12/users/user_1/workspace", None, None, None
        )
        assert result["workspace_path"] == "/workspace"


class TestMountOperations:
    """Test mount operation path scoping."""

    def test_add_mount(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test add_mount with path scoping."""
        mock_fs.add_mount.return_value = "mount-123"
        result = scoped_fs.add_mount("/external/gcs", "gcs", {"bucket": "my-bucket"})
        mock_fs.add_mount.assert_called_once_with(
            "/tenants/team_12/users/user_1/external/gcs",
            "gcs",
            {"bucket": "my-bucket"},
            0,
            False,
        )
        assert result == "mount-123"

    def test_list_mounts(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test list_mounts unscopes paths."""
        mock_fs.list_mounts.return_value = [
            {"mount_point": "/tenants/team_12/users/user_1/external/gcs"}
        ]
        result = scoped_fs.list_mounts()
        assert result[0]["mount_point"] == "/external/gcs"


class TestSandboxOperations:
    """Test that sandbox operations are passed through without path scoping."""

    def test_sandbox_create(self, scoped_fs: ScopedFilesystem, mock_fs: MagicMock) -> None:
        """Test sandbox_create is passed through."""
        mock_fs.sandbox_create.return_value = {"sandbox_id": "sb-123"}
        result = scoped_fs.sandbox_create("test-sandbox")
        mock_fs.sandbox_create.assert_called_once_with("test-sandbox", 10, "e2b", None, None)
        assert result["sandbox_id"] == "sb-123"


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
