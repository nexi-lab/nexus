"""Sync/async parity tests.

Validates that SlimNexusFS (async) and SyncNexusFS (sync) produce
identical results for all public API methods on the same backend.

Uses a shared test class pattern (like httpx, httpcore) to avoid
duplicating test logic.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import PathRouter
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore
from nexus.fs._sync import SyncNexusFS


def _build_fs(tmp_path: Path) -> SlimNexusFS:
    """Build a SlimNexusFS with a real local backend."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    from nexus.core.mount_table import MountTable

    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        router=router,
    )
    kernel._init_cred = OperationContext(
        user_id="test",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel._driver_coordinator.mount("/local", backend)
    metastore.put(_make_mount_entry("/local", backend.name))

    return SlimNexusFS(kernel)


# ── Async tests ───────────────────────────────────────────────────────────


@pytest.fixture
def async_fs(tmp_path: Path) -> SlimNexusFS:
    return _build_fs(tmp_path)


class TestAsyncOperations:
    """Full lifecycle via the SlimNexusFS API (now sync)."""

    def test_write_read_parity(self, async_fs: SlimNexusFS):
        content = b"parity test content"
        async_fs.write("/local/parity.txt", content)
        result = async_fs.read("/local/parity.txt")
        assert result == content

    def test_stat_parity(self, async_fs: SlimNexusFS):
        async_fs.write("/local/stat.txt", b"stat content")
        stat = async_fs.stat("/local/stat.txt")
        assert stat is not None
        assert stat["size"] == 12
        assert stat["is_directory"] is False
        assert stat["path"] == "/local/stat.txt"

    def test_ls_parity(self, async_fs: SlimNexusFS):
        async_fs.write("/local/ls_a.txt", b"a")
        async_fs.write("/local/ls_b.txt", b"b")
        entries = async_fs.ls("/local/", detail=False, recursive=True)
        paths = sorted(e for e in entries if e.endswith(".txt"))
        assert "/local/ls_a.txt" in paths
        assert "/local/ls_b.txt" in paths

    def test_exists_parity(self, async_fs: SlimNexusFS):
        assert not async_fs.exists("/local/nope.txt")
        async_fs.write("/local/nope.txt", b"now")
        assert async_fs.exists("/local/nope.txt")

    def test_delete_parity(self, async_fs: SlimNexusFS):
        async_fs.write("/local/del.txt", b"bye")
        async_fs.delete("/local/del.txt")
        assert async_fs.stat("/local/del.txt") is None

    def test_rename_parity(self, async_fs: SlimNexusFS):
        async_fs.write("/local/old_p.txt", b"rename")
        async_fs.rename("/local/old_p.txt", "/local/new_p.txt")
        assert async_fs.read("/local/new_p.txt") == b"rename"

    def test_copy_parity(self, async_fs: SlimNexusFS):
        async_fs.write("/local/cp_src.txt", b"copy")
        async_fs.copy("/local/cp_src.txt", "/local/cp_dst.txt")
        assert async_fs.read("/local/cp_dst.txt") == b"copy"

    def test_mkdir_parity(self, async_fs: SlimNexusFS):
        async_fs.mkdir("/local/parity_dir")
        stat = async_fs.stat("/local/parity_dir")
        assert stat is not None
        assert stat["is_directory"] is True

    def test_list_mounts_parity(self, async_fs: SlimNexusFS):
        assert "/local" in async_fs.list_mounts()

    def test_read_range_parity(self, async_fs: SlimNexusFS):
        async_fs.write("/local/range.txt", b"0123456789")
        result = async_fs.read_range("/local/range.txt", 2, 7)
        assert result == b"23456"


# ── Sync tests (must produce identical results) ──────────────────────────


@pytest.fixture
def sync_fs(tmp_path: Path) -> SyncNexusFS:
    async_facade = _build_fs(tmp_path)
    return SyncNexusFS(async_facade)


class TestSyncOperations:
    """Same operations via the sync SyncNexusFS wrapper."""

    def test_write_read_parity(self, sync_fs: SyncNexusFS):
        content = b"parity test content"
        sync_fs.write("/local/parity.txt", content)
        result = sync_fs.read("/local/parity.txt")
        assert result == content

    def test_stat_parity(self, sync_fs: SyncNexusFS):
        sync_fs.write("/local/stat.txt", b"stat content")
        stat = sync_fs.stat("/local/stat.txt")
        assert stat is not None
        assert stat["size"] == 12
        assert stat["is_directory"] is False
        assert stat["path"] == "/local/stat.txt"

    def test_ls_parity(self, sync_fs: SyncNexusFS):
        sync_fs.write("/local/ls_a.txt", b"a")
        sync_fs.write("/local/ls_b.txt", b"b")
        entries = sync_fs.ls("/local/", detail=False)
        paths = sorted(e for e in entries if e.endswith(".txt"))
        assert "/local/ls_a.txt" in paths
        assert "/local/ls_b.txt" in paths

    def test_exists_parity(self, sync_fs: SyncNexusFS):
        assert not sync_fs.exists("/local/nope.txt")
        sync_fs.write("/local/nope.txt", b"now")
        assert sync_fs.exists("/local/nope.txt")

    def test_delete_parity(self, sync_fs: SyncNexusFS):
        sync_fs.write("/local/del.txt", b"bye")
        sync_fs.delete("/local/del.txt")
        assert sync_fs.stat("/local/del.txt") is None

    def test_rename_parity(self, sync_fs: SyncNexusFS):
        sync_fs.write("/local/old_p.txt", b"rename")
        sync_fs.rename("/local/old_p.txt", "/local/new_p.txt")
        assert sync_fs.read("/local/new_p.txt") == b"rename"

    def test_copy_parity(self, sync_fs: SyncNexusFS):
        sync_fs.write("/local/cp_src.txt", b"copy")
        sync_fs.copy("/local/cp_src.txt", "/local/cp_dst.txt")
        assert sync_fs.read("/local/cp_dst.txt") == b"copy"

    def test_mkdir_parity(self, sync_fs: SyncNexusFS):
        sync_fs.mkdir("/local/parity_dir")
        stat = sync_fs.stat("/local/parity_dir")
        assert stat is not None
        assert stat["is_directory"] is True

    def test_list_mounts_parity(self, sync_fs: SyncNexusFS):
        assert "/local" in sync_fs.list_mounts()

    def test_read_range_parity(self, sync_fs: SyncNexusFS):
        sync_fs.write("/local/range.txt", b"0123456789")
        result = sync_fs.read_range("/local/range.txt", 2, 7)
        assert result == b"23456"


# ── Running event loop context (Jupyter-like) ────────────────────────────


class TestRunningLoopContext:
    """Verify SyncNexusFS works inside an already-running event loop."""

    @pytest.mark.asyncio
    async def test_sync_inside_async_via_thread(self, tmp_path: Path):
        """SyncNexusFS must work when called from a worker thread
        while an event loop is running (Jupyter notebook scenario)."""
        async_facade = _build_fs(tmp_path)
        sync_wrapper = SyncNexusFS(async_facade)

        def _sync_work():
            sync_wrapper.write("/local/jupyter.txt", b"from thread")
            return sync_wrapper.read("/local/jupyter.txt")

        result = await anyio.to_thread.run_sync(_sync_work)
        assert result == b"from thread"
        sync_wrapper.close()
