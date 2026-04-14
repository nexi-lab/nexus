"""Sync parity tests.

Validates that SlimNexusFS and SyncNexusFS produce identical results
for all public API methods on the same backend.

Uses a shared test class pattern (like httpx, httpcore) to avoid
duplicating test logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
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

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel._driver_coordinator.mount("/local", backend)
    metastore.put(_make_mount_entry("/local", backend.name))

    return SlimNexusFS(kernel)


# ── SlimNexusFS tests (sync) ──────────────────────────────────────────


@pytest.fixture
def slim_fs(tmp_path: Path) -> SlimNexusFS:
    return _build_fs(tmp_path)


class TestSlimOperations:
    """Full lifecycle via the sync SlimNexusFS API."""

    def test_write_read_parity(self, slim_fs: SlimNexusFS):
        content = b"parity test content"
        slim_fs.write("/local/parity.txt", content)
        result = slim_fs.read("/local/parity.txt")
        assert result == content

    def test_stat_parity(self, slim_fs: SlimNexusFS):
        slim_fs.write("/local/stat.txt", b"stat content")
        stat = slim_fs.stat("/local/stat.txt")
        assert stat is not None
        assert stat["size"] == 12
        assert stat["is_directory"] is False
        assert stat["path"] == "/local/stat.txt"

    def test_ls_parity(self, slim_fs: SlimNexusFS):
        slim_fs.write("/local/ls_a.txt", b"a")
        slim_fs.write("/local/ls_b.txt", b"b")
        entries = slim_fs.ls("/local/", detail=False, recursive=True)
        paths = sorted(e for e in entries if e.endswith(".txt"))
        assert "/local/ls_a.txt" in paths
        assert "/local/ls_b.txt" in paths

    def test_exists_parity(self, slim_fs: SlimNexusFS):
        assert not slim_fs.exists("/local/nope.txt")
        slim_fs.write("/local/nope.txt", b"now")
        assert slim_fs.exists("/local/nope.txt")

    def test_delete_parity(self, slim_fs: SlimNexusFS):
        slim_fs.write("/local/del.txt", b"bye")
        slim_fs.delete("/local/del.txt")
        assert slim_fs.stat("/local/del.txt") is None

    def test_rename_parity(self, slim_fs: SlimNexusFS):
        slim_fs.write("/local/old_p.txt", b"rename")
        slim_fs.rename("/local/old_p.txt", "/local/new_p.txt")
        assert slim_fs.read("/local/new_p.txt") == b"rename"

    def test_copy_parity(self, slim_fs: SlimNexusFS):
        slim_fs.write("/local/cp_src.txt", b"copy")
        slim_fs.copy("/local/cp_src.txt", "/local/cp_dst.txt")
        assert slim_fs.read("/local/cp_dst.txt") == b"copy"

    def test_mkdir_parity(self, slim_fs: SlimNexusFS):
        slim_fs.mkdir("/local/parity_dir")
        stat = slim_fs.stat("/local/parity_dir")
        assert stat is not None
        assert stat["is_directory"] is True

    def test_list_mounts_parity(self, slim_fs: SlimNexusFS):
        assert "/local" in slim_fs.list_mounts()

    def test_read_range_parity(self, slim_fs: SlimNexusFS):
        slim_fs.write("/local/range.txt", b"0123456789")
        result = slim_fs.read_range("/local/range.txt", 2, 7)
        assert result == b"23456"


# ── Sync tests (must produce identical results) ──────────────────────────


@pytest.fixture
def sync_fs(tmp_path: Path) -> SyncNexusFS:
    slim_facade = _build_fs(tmp_path)
    return SyncNexusFS(slim_facade)


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
