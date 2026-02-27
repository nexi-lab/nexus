"""Integration tests: KernelDispatch + PipeResolver end-to-end (#1201).

Verifies that KernelDispatch.resolve_read/write/delete correctly
dispatches to PipeResolver and short-circuits the normal VFS pipeline.

See: core/kernel_dispatch.py, system_services/pipe_resolver.py
"""

from __future__ import annotations

import pytest

from nexus.contracts.metadata import DT_REG, FileMetadata
from nexus.core.kernel_dispatch import KernelDispatch
from nexus.core.pipe import PipeFullError
from nexus.system_services.pipe_manager import PipeManager
from nexus.system_services.pipe_resolver import PipeResolver

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


def _make_dispatch() -> tuple[KernelDispatch, PipeManager, MockMetastore]:
    ms = MockMetastore()
    mgr = PipeManager(ms, zone_id="test-zone")
    resolver = PipeResolver(pipe_manager=mgr, metastore=ms)
    dispatch = KernelDispatch()
    dispatch.register_resolver(resolver)
    return dispatch, mgr, ms


# ======================================================================
# resolve_read
# ======================================================================


class TestDispatchRead:
    def test_pipe_read_handled(self) -> None:
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/inbox", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/inbox", b"message")

        handled, result = dispatch.resolve_read("/nexus/pipes/inbox")
        assert handled is True
        assert result == b"message"

    def test_pipe_read_empty_returns_empty_bytes(self) -> None:
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/empty", capacity=1024)

        handled, result = dispatch.resolve_read("/nexus/pipes/empty")
        assert handled is True
        assert result == b""

    def test_pipe_read_with_metadata(self) -> None:
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/meta", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/meta", b"data")

        handled, result = dispatch.resolve_read("/nexus/pipes/meta", return_metadata=True)
        assert handled is True
        assert isinstance(result, dict)
        assert result["content"] == b"data"
        assert "pipe_stats" in result

    def test_regular_file_not_handled(self) -> None:
        dispatch, _, ms = _make_dispatch()
        ms.put(
            FileMetadata(
                path="/data/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=100,
                entry_type=DT_REG,
            )
        )

        handled, result = dispatch.resolve_read("/data/file.txt")
        assert handled is False
        assert result is None

    def test_nonexistent_path_not_handled(self) -> None:
        dispatch, _, _ = _make_dispatch()
        handled, result = dispatch.resolve_read("/does/not/exist")
        assert handled is False
        assert result is None


# ======================================================================
# resolve_write
# ======================================================================


class TestDispatchWrite:
    def test_pipe_write_handled(self) -> None:
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/out", capacity=1024)

        handled, result = dispatch.resolve_write("/nexus/pipes/out", b"payload")
        assert handled is True
        assert isinstance(result, dict)
        assert result["size"] == 7

    def test_pipe_write_then_read(self) -> None:
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/roundtrip", capacity=1024)

        dispatch.resolve_write("/nexus/pipes/roundtrip", b"hello")
        handled, result = dispatch.resolve_read("/nexus/pipes/roundtrip")
        assert handled is True
        assert result == b"hello"

    def test_regular_file_not_handled(self) -> None:
        dispatch, _, _ = _make_dispatch()
        handled, result = dispatch.resolve_write("/data/file.txt", b"content")
        assert handled is False
        assert result is None

    def test_pipe_write_full_raises(self) -> None:
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/tiny", capacity=10)
        mgr.pipe_write_nowait("/nexus/pipes/tiny", b"x" * 10)

        with pytest.raises(PipeFullError):
            dispatch.resolve_write("/nexus/pipes/tiny", b"overflow")


# ======================================================================
# resolve_delete
# ======================================================================


class TestDispatchDelete:
    def test_pipe_delete_handled(self) -> None:
        dispatch, mgr, ms = _make_dispatch()
        buf = mgr.create("/nexus/pipes/delme", capacity=1024)

        handled, result = dispatch.resolve_delete("/nexus/pipes/delme")
        assert handled is True
        assert buf.closed is True
        assert ms.get("/nexus/pipes/delme") is None

    def test_regular_file_not_handled(self) -> None:
        dispatch, _, _ = _make_dispatch()
        handled, result = dispatch.resolve_delete("/data/file.txt")
        assert handled is False
        assert result is None

    def test_pipe_delete_nonexistent_not_handled(self) -> None:
        """Non-existent pipe doesn't match → dispatch returns (False, None)."""
        dispatch, _, _ = _make_dispatch()
        handled, result = dispatch.resolve_delete("/nexus/pipes/ghost")
        assert handled is False
        assert result is None


# ======================================================================
# Resolver ordering — pipe resolver is first-match
# ======================================================================


class TestResolverOrdering:
    def test_pipe_resolver_is_registered(self) -> None:
        dispatch, _, _ = _make_dispatch()
        assert dispatch.resolver_count == 1

    def test_multiple_resolvers_first_match_wins(self) -> None:
        """Pipe resolver registered first should short-circuit."""
        dispatch, mgr, _ = _make_dispatch()
        mgr.create("/nexus/pipes/test", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/test", b"from-pipe")

        # Register a second catch-all resolver
        class CatchAll:
            def matches(self, path: str) -> bool:
                return True

            def read(self, path: str, **kw) -> bytes:
                return b"from-catchall"

            def write(self, path: str, content: bytes) -> dict:
                return {}

            def delete(self, path: str, **kw) -> None:
                pass

        dispatch.register_resolver(CatchAll())

        # Pipe resolver should win (registered first)
        handled, result = dispatch.resolve_read("/nexus/pipes/test")
        assert handled is True
        assert result == b"from-pipe"
