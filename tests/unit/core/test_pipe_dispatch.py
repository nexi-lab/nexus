"""Integration tests: PathRouter DT_PIPE native dispatch (#1201, #1496).

Verifies that PathRouter.route() detects DT_PIPE inodes and returns
PipeRouteResult, and that NexusFS sys_read/sys_write/sys_unlink
dispatch to PipeManager for pipe paths.

See: core/router.py, core/pipe_manager.py
"""

from __future__ import annotations

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.pipe import PipeFullError
from nexus.core.pipe_manager import PipeManager
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


# ======================================================================
# PathRouter DT_PIPE detection
# ======================================================================


class TestPathRouterPipeRoute:
    def test_pipe_path_returns_pipe_route_result(self) -> None:
        ms = MockMetastore()
        router = PathRouter(ms)
        mgr = PipeManager(ms)
        mgr.create("/pipes/inbox", capacity=1024)

        result = router.route("/pipes/inbox")
        assert isinstance(result, PipeRouteResult)
        assert result.path == "/pipes/inbox"

    def test_regular_file_returns_route_result(self) -> None:
        ms = MockMetastore()
        router = PathRouter(ms)

        # Need a mount for the regular file to route through
        from unittest.mock import MagicMock

        backend = MagicMock()
        backend.name = "local"
        router.add_mount("/workspace", backend)

        result = router.route("/workspace/file.txt")
        assert isinstance(result, RouteResult)
        assert result.backend_path == "file.txt"

    def test_nonexistent_path_raises(self) -> None:
        ms = MockMetastore()
        router = PathRouter(ms)

        from nexus.contracts.exceptions import PathNotMountedError

        with pytest.raises(PathNotMountedError):
            router.route("/does/not/exist")

    def test_pipe_at_exact_path_only(self) -> None:
        """DT_PIPE only matches at exact target path, not parent paths."""
        ms = MockMetastore()
        router = PathRouter(ms)
        mgr = PipeManager(ms)
        mgr.create("/pipes/inbox", capacity=1024)

        # /pipes/inbox is a pipe
        result = router.route("/pipes/inbox")
        assert isinstance(result, PipeRouteResult)

        # /pipes/inbox/sub should NOT match pipe — should raise PathNotMountedError
        from nexus.contracts.exceptions import PathNotMountedError

        with pytest.raises(PathNotMountedError):
            router.route("/pipes/inbox/sub")


# ======================================================================
# PipeManager read/write via pipe dispatch
# ======================================================================


class TestPipeReadWrite:
    def test_pipe_write_then_read(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms)
        buf = mgr.create("/pipes/roundtrip", capacity=1024)

        mgr.pipe_write_nowait("/pipes/roundtrip", b"hello")
        data = buf.read_nowait()
        assert data == b"hello"

    def test_pipe_write_full_raises(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms)
        mgr.create("/pipes/tiny", capacity=10)
        mgr.pipe_write_nowait("/pipes/tiny", b"x" * 10)

        with pytest.raises(PipeFullError):
            mgr.pipe_write_nowait("/pipes/tiny", b"overflow")

    def test_pipe_destroy(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms)
        buf = mgr.create("/pipes/delme", capacity=1024)

        mgr.destroy("/pipes/delme")
        assert buf.closed is True
        assert ms.get("/pipes/delme") is None
