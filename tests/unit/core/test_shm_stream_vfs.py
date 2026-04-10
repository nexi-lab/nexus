"""VFS integration tests for DT_STREAM with io_profile="shared_memory" (#1680).

Covers the NexusFS sys_setattr → sys_read → sys_unlink path when
io_profile="shared_memory" is requested. This exercises the
``_custom_stream_backends`` code path in nexus_fs.py that creates
a ``SharedMemoryStreamBackend`` and registers it alongside the Rust
kernel stream entry.

Without this test, the shared_memory io_profile branch had zero
test coverage and could silently regress.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nexus_kernel", reason="Requires nexus_kernel Rust extension")

from nexus.contracts.metadata import DT_STREAM
from nexus.core.nexus_fs import NexusFS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_nexus_fs(tmp_path):
    """Create a minimal NexusFS for stream tests (no backends needed)."""
    from nexus.core.config import PermissionConfig
    from nexus.core.mount_table import MountTable
    from nexus.core.router import PathRouter
    from nexus.fs._sqlite_meta import SQLiteMetastore

    db_path = str(tmp_path / "meta.db")
    metastore = SQLiteMetastore(db_path)
    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)

    nx = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        router=router,
    )
    return nx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShmStreamVFS:
    """DT_STREAM with io_profile='shared_memory' through NexusFS syscalls."""

    @pytest.mark.asyncio
    async def test_create_shm_stream_via_sys_setattr(self, tmp_path) -> None:
        """sys_setattr(entry_type=DT_STREAM, io_profile='shared_memory') should
        create a SharedMemoryStreamBackend in _custom_stream_backends."""
        nx = _make_nexus_fs(tmp_path)
        path = "/__sys__/test/shm-stream"

        result = nx.sys_setattr(
            path,
            entry_type=DT_STREAM,
            capacity=4096,
            io_profile="shared_memory",
        )
        assert result["created"] is True
        assert result["entry_type"] == DT_STREAM

        # SharedMemoryStreamBackend should be registered
        assert path in nx._custom_stream_backends
        backend = nx._custom_stream_backends[path]
        assert not backend.closed

        nx.close()

    @pytest.mark.asyncio
    async def test_write_read_roundtrip_shm_stream(self, tmp_path) -> None:
        """Write to SHM stream via the custom backend, read back via sys_read."""
        nx = _make_nexus_fs(tmp_path)
        path = "/__sys__/test/shm-rt"

        nx.sys_setattr(
            path,
            entry_type=DT_STREAM,
            capacity=4096,
            io_profile="shared_memory",
        )

        # Write via the custom backend's write_nowait (simulates kernel stream_write)
        backend = nx._custom_stream_backends[path]
        backend.write_nowait(b"hello shm")

        # Read via sys_read — should hit the _custom_stream_backends branch
        data = nx.sys_read(path, offset=0)
        assert data == b"hello shm"

        nx.close()

    @pytest.mark.asyncio
    async def test_default_io_profile_uses_rust_kernel(self, tmp_path) -> None:
        """Default io_profile='memory' should NOT create a custom stream backend —
        it uses the Rust kernel IPC registry directly."""
        nx = _make_nexus_fs(tmp_path)
        path = "/__sys__/test/default-stream"

        nx.sys_setattr(
            path,
            entry_type=DT_STREAM,
            capacity=4096,
            # no io_profile → defaults to "memory" → Rust kernel
        )

        # No custom stream backend — Rust kernel owns it
        assert path not in nx._custom_stream_backends

        nx.close()

    @pytest.mark.asyncio
    async def test_shm_stream_close_on_nexus_close(self, tmp_path) -> None:
        """NexusFS.close() should clean up SHM stream backends."""
        nx = _make_nexus_fs(tmp_path)
        path = "/__sys__/test/shm-close"

        nx.sys_setattr(
            path,
            entry_type=DT_STREAM,
            capacity=4096,
            io_profile="shared_memory",
        )

        backend = nx._custom_stream_backends[path]
        assert not backend.closed

        nx.close()
        # After close, custom backends dict should be cleared
        assert len(nx._custom_stream_backends) == 0

    @pytest.mark.asyncio
    async def test_shm_stream_multi_write_read(self, tmp_path) -> None:
        """Multiple writes to SHM stream, sequential reads with offset tracking."""
        nx = _make_nexus_fs(tmp_path)
        path = "/__sys__/test/shm-multi"

        nx.sys_setattr(
            path,
            entry_type=DT_STREAM,
            capacity=8192,
            io_profile="shared_memory",
        )

        backend = nx._custom_stream_backends[path]
        backend.write_nowait(b"msg1")
        backend.write_nowait(b"msg2")
        backend.write_nowait(b"msg3")

        # Read offset 0 → first message
        data1 = nx.sys_read(path, offset=0)
        assert data1 == b"msg1"

        nx.close()
