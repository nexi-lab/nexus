"""Unit tests for PipeResolver — VFSPathResolver for DT_PIPE paths (#1201).

Tests the PipeResolver in isolation: matches/read/write/delete for
active pipes, metastore-only pipes, non-pipes, empty pipes, and
error mapping.

See: src/nexus/system_services/pipe_resolver.py
"""

from __future__ import annotations

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.metadata import DT_REG, FileMetadata
from nexus.core.pipe import PipeFullError
from nexus.system_services.pipe_manager import PipeManager
from nexus.system_services.pipe_resolver import PipeResolver

# ======================================================================
# Shared mock
# ======================================================================


class MockMetastore:
    """Minimal MetastoreABC mock (same pattern as test_pipe.py)."""

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


def _make_resolver() -> tuple[PipeResolver, PipeManager, MockMetastore]:
    ms = MockMetastore()
    mgr = PipeManager(ms, zone_id="test-zone")
    resolver = PipeResolver(pipe_manager=mgr, metastore=ms)
    return resolver, mgr, ms


# ======================================================================
# matches()
# ======================================================================


class TestPipeResolverMatches:
    def test_matches_active_pipe(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/inbox", capacity=1024)
        assert resolver.matches("/nexus/pipes/inbox") is True

    def test_no_match_for_regular_file(self) -> None:
        resolver, _, ms = _make_resolver()
        ms.put(
            FileMetadata(
                path="/data/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=100,
                entry_type=DT_REG,
            )
        )
        assert resolver.matches("/data/file.txt") is False

    def test_no_match_for_nonexistent_path(self) -> None:
        resolver, _, _ = _make_resolver()
        assert resolver.matches("/does/not/exist") is False

    def test_matches_metastore_only_pipe(self) -> None:
        """Pipe inode in metastore but buffer lost (restart recovery)."""
        resolver, mgr, ms = _make_resolver()
        mgr.create("/nexus/pipes/recovered", capacity=1024)
        # Simulate buffer loss (restart)
        mgr._buffers.clear()

        # Should still match via metastore fallback
        assert resolver.matches("/nexus/pipes/recovered") is True


# ======================================================================
# read()
# ======================================================================


class TestPipeResolverRead:
    def test_read_active_pipe(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/inbox", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/inbox", b"hello")

        result = resolver.read("/nexus/pipes/inbox")
        assert result == b"hello"

    def test_read_empty_pipe_returns_empty_bytes(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/empty", capacity=1024)

        result = resolver.read("/nexus/pipes/empty")
        assert result == b""

    def test_read_with_metadata(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/meta", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/meta", b"msg1")
        mgr.pipe_write_nowait("/nexus/pipes/meta", b"msg2")

        result = resolver.read("/nexus/pipes/meta", return_metadata=True)
        assert isinstance(result, dict)
        assert result["content"] == b"msg1"
        assert result["size"] == 4
        assert "pipe_stats" in result

    def test_read_nonexistent_raises(self) -> None:
        resolver, _, _ = _make_resolver()
        with pytest.raises(NexusFileNotFoundError):
            resolver.read("/nexus/pipes/ghost")

    def test_read_closed_pipe_raises(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/closed", capacity=1024)
        buf = mgr._get_buffer("/nexus/pipes/closed")
        buf.close()

        with pytest.raises(NexusFileNotFoundError, match="closed"):
            resolver.read("/nexus/pipes/closed")

    def test_read_metastore_recovery(self) -> None:
        """Read from pipe whose buffer was lost but inode persists."""
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/recover", capacity=1024)
        # Simulate buffer loss
        mgr._buffers.clear()

        # Should recover via open() and return empty (new buffer)
        result = resolver.read("/nexus/pipes/recover")
        assert result == b""

    def test_read_fifo_ordering(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/fifo", capacity=4096)
        mgr.pipe_write_nowait("/nexus/pipes/fifo", b"first")
        mgr.pipe_write_nowait("/nexus/pipes/fifo", b"second")
        mgr.pipe_write_nowait("/nexus/pipes/fifo", b"third")

        assert resolver.read("/nexus/pipes/fifo") == b"first"
        assert resolver.read("/nexus/pipes/fifo") == b"second"
        assert resolver.read("/nexus/pipes/fifo") == b"third"
        assert resolver.read("/nexus/pipes/fifo") == b""


# ======================================================================
# write()
# ======================================================================


class TestPipeResolverWrite:
    def test_write_to_active_pipe(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/out", capacity=1024)

        result = resolver.write("/nexus/pipes/out", b"payload")
        assert isinstance(result, dict)
        assert result["size"] == 7
        assert "etag" in result
        assert "version" in result

    def test_write_then_read(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/roundtrip", capacity=1024)

        resolver.write("/nexus/pipes/roundtrip", b"hello")
        data = resolver.read("/nexus/pipes/roundtrip")
        assert data == b"hello"

    def test_write_nonexistent_raises(self) -> None:
        resolver, _, _ = _make_resolver()
        with pytest.raises(NexusFileNotFoundError):
            resolver.write("/nexus/pipes/ghost", b"data")

    def test_write_full_pipe_raises_pipe_full(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/tiny", capacity=10)
        mgr.pipe_write_nowait("/nexus/pipes/tiny", b"x" * 10)  # fill

        with pytest.raises(PipeFullError):
            resolver.write("/nexus/pipes/tiny", b"overflow")

    def test_write_closed_pipe_raises(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/closed", capacity=1024)
        mgr._get_buffer("/nexus/pipes/closed").close()

        with pytest.raises(NexusFileNotFoundError, match="closed"):
            resolver.write("/nexus/pipes/closed", b"data")


# ======================================================================
# delete()
# ======================================================================


class TestPipeResolverDelete:
    def test_delete_active_pipe(self) -> None:
        resolver, mgr, ms = _make_resolver()
        buf = mgr.create("/nexus/pipes/delme", capacity=1024)

        resolver.delete("/nexus/pipes/delme")

        assert buf.closed is True
        assert ms.get("/nexus/pipes/delme") is None
        assert "/nexus/pipes/delme" not in mgr._buffers

    def test_delete_nonexistent_raises(self) -> None:
        resolver, _, _ = _make_resolver()
        with pytest.raises(NexusFileNotFoundError):
            resolver.delete("/nexus/pipes/ghost")

    def test_delete_then_read_raises(self) -> None:
        resolver, mgr, _ = _make_resolver()
        mgr.create("/nexus/pipes/gone", capacity=1024)
        resolver.delete("/nexus/pipes/gone")

        with pytest.raises(NexusFileNotFoundError):
            resolver.read("/nexus/pipes/gone")


# ======================================================================
# Protocol conformance
# ======================================================================


class TestPipeResolverProtocol:
    def test_implements_vfs_path_resolver(self) -> None:
        from nexus.contracts.vfs_hooks import VFSPathResolver

        resolver, _, _ = _make_resolver()
        assert isinstance(resolver, VFSPathResolver)
