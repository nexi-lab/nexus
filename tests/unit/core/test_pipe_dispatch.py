"""Integration tests: PathRouter DT_PIPE native dispatch (#1201, #1496).

Verifies that PathRouter.route() detects DT_PIPE inodes and returns
PipeRouteResult, and that Rust kernel IPC pipe operations (create_pipe,
pipe_write_nowait, pipe_read_nowait, close_pipe) work correctly.

See: core/router.py, Rust kernel IPC registry
"""

from __future__ import annotations

import pytest

from nexus.contracts.metadata import DT_PIPE, FileMetadata
from nexus.core.router import PathRouter, PipeRouteResult, RouteResult

# ======================================================================
# Shared mock + setup
# ======================================================================


class MockMetastore:
    """Minimal MetastoreABC mock."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> None:
        if metadata.path:
            self._store[metadata.path] = metadata

    def delete(self, path: str, *, consistency: str = "sc") -> dict | None:
        return {"path": path} if self._store.pop(path, None) else None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs) -> list:  # noqa: ARG002
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def close(self) -> None:
        pass


def _create_pipe_inode(ms: MockMetastore, path: str) -> None:
    """Register a DT_PIPE inode in the metastore (replaces PipeManager.create)."""
    ms.put(
        FileMetadata(
            path=path,
            backend_name="pipe",
            physical_path="",
            size=0,
            etag="",
            mime_type="application/octet-stream",
            entry_type=DT_PIPE,
            zone_id="test",
        )
    )


# ======================================================================
# PathRouter DT_PIPE detection
# ======================================================================


class TestPathRouterPipeRoute:
    def test_pipe_path_returns_pipe_route_result(self) -> None:
        ms = MockMetastore()
        from nexus.core.mount_table import MountTable

        mount_table = MountTable(ms)
        router = PathRouter(mount_table)
        _create_pipe_inode(ms, "/pipes/inbox")

        result = router.route("/pipes/inbox")
        assert isinstance(result, PipeRouteResult)
        assert result.path == "/pipes/inbox"

    def test_regular_file_returns_route_result(self) -> None:
        ms = MockMetastore()

        # Need a mount for the regular file to route through
        from unittest.mock import MagicMock

        from nexus.core.mount_table import MountTable

        mount_table = MountTable(ms)
        router = PathRouter(mount_table)

        backend = MagicMock()
        backend.name = "local"
        mount_table.add("/workspace", backend)

        result = router.route("/workspace/file.txt")
        assert isinstance(result, RouteResult)
        assert result.backend_path == "file.txt"

    def test_nonexistent_path_raises(self) -> None:
        ms = MockMetastore()
        from nexus.core.mount_table import MountTable

        mount_table = MountTable(ms)
        router = PathRouter(mount_table)

        from nexus.contracts.exceptions import PathNotMountedError

        with pytest.raises(PathNotMountedError):
            router.route("/does/not/exist")

    def test_pipe_at_exact_path_only(self) -> None:
        """DT_PIPE only matches at exact target path, not parent paths."""
        ms = MockMetastore()
        from nexus.core.mount_table import MountTable

        mount_table = MountTable(ms)
        router = PathRouter(mount_table)
        _create_pipe_inode(ms, "/pipes/inbox")

        # /pipes/inbox is a pipe
        result = router.route("/pipes/inbox")
        assert isinstance(result, PipeRouteResult)

        # /pipes/inbox/sub should NOT match pipe — should raise PathNotMountedError
        from nexus.contracts.exceptions import PathNotMountedError

        with pytest.raises(PathNotMountedError):
            router.route("/pipes/inbox/sub")


# ======================================================================
# Kernel IPC pipe read/write (replaces PipeManager tests)
# ======================================================================


class TestKernelPipeReadWrite:
    def _make_kernel(self):
        """Create a Rust Kernel instance for IPC testing."""
        from nexus_kernel import Kernel

        return Kernel()

    def test_pipe_write_then_read(self) -> None:
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/roundtrip", 1024)

        kernel.pipe_write_nowait("/pipes/roundtrip", b"hello")
        data = kernel.pipe_read_nowait("/pipes/roundtrip")
        assert bytes(data) == b"hello"

        kernel.close_all_pipes()

    def test_pipe_write_full_raises(self) -> None:
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/tiny", 10)
        kernel.pipe_write_nowait("/pipes/tiny", b"x" * 10)

        with pytest.raises(RuntimeError, match="PipeFull"):
            kernel.pipe_write_nowait("/pipes/tiny", b"overflow")

        kernel.close_all_pipes()

    def test_pipe_close(self) -> None:
        """close_pipe() signals the closed flag (pipe still registered);
        destroy_pipe() removes it from the registry.
        """
        kernel = self._make_kernel()
        kernel.create_pipe("/pipes/delme", 1024)

        # close_pipe: sets closed flag but does NOT remove from registry
        kernel.close_pipe("/pipes/delme")
        assert "/pipes/delme" in kernel.list_pipes()

        # destroy_pipe: removes from registry
        kernel.destroy_pipe("/pipes/delme")
        assert "/pipes/delme" not in kernel.list_pipes()

        kernel.close_all_pipes()
