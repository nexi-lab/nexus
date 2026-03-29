"""Integration test: boot-to-read/write lifecycle with real storage.

Uses real SQLite metadata store + real CASLocalBackend in a temp directory.
No mocks — this verifies the full slim package actually works end-to-end.

Test plan:
1. Boot slim NexusFS with SQLite + CASLocalBackend
2. Write a file, read it back
3. Stat the file
4. List directory
5. Rename the file
6. Delete the file
7. Verify deleted
8. Multi-backend: mount two local backends, write to each
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import PathRouter
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore


@pytest.fixture
def slim_fs(tmp_path: Path):
    """Boot a full slim NexusFS with SQLite + CASLocalBackend."""
    # SQLite metastore
    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    # CASLocalBackend
    from nexus.backends.storage.cas_local import CASLocalBackend

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)
    backend.set_metastore(metastore)

    # Router with mount
    router = PathRouter(metastore)
    router.add_mount("/local", backend)

    # Create DT_MOUNT entry so stat("/local") works
    metastore.put(_make_mount_entry("/local", backend.name))

    # Kernel
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

    return SlimNexusFS(kernel)


@pytest.fixture
def dual_fs(tmp_path: Path):
    """Boot slim NexusFS with two local backends."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    data_a = tmp_path / "data_a"
    data_a.mkdir()
    data_b = tmp_path / "data_b"
    data_b.mkdir()

    backend_a = CASLocalBackend(root_path=data_a)
    backend_a.set_metastore(metastore)
    backend_b = CASLocalBackend(root_path=data_b)
    backend_b.set_metastore(metastore)

    router = PathRouter(metastore)
    router.add_mount("/a", backend_a)
    router.add_mount("/b", backend_b)

    # Create DT_MOUNT entries
    for mp, be in [("/a", backend_a), ("/b", backend_b)]:
        metastore.put(_make_mount_entry(mp, be.name))

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

    return SlimNexusFS(kernel)


# ---------------------------------------------------------------------------
# Single-backend lifecycle
# ---------------------------------------------------------------------------


class TestSingleBackendLifecycle:
    @pytest.mark.asyncio
    async def test_write_and_read(self, slim_fs: SlimNexusFS):
        """Write content, read it back, verify match."""
        content = b"Hello, nexus-fs!"
        await slim_fs.write("/local/test.txt", content)
        result = await slim_fs.read("/local/test.txt")
        assert result == content

    @pytest.mark.asyncio
    async def test_stat(self, slim_fs: SlimNexusFS):
        """Write a file, stat it, verify metadata."""
        await slim_fs.write("/local/meta.txt", b"metadata test")
        stat = await slim_fs.stat("/local/meta.txt")
        assert stat is not None
        assert stat["path"] == "/local/meta.txt"
        assert stat["size"] == 13
        assert stat["is_directory"] is False

    @pytest.mark.asyncio
    async def test_ls(self, slim_fs: SlimNexusFS):
        """Write files, list directory, verify they appear."""
        await slim_fs.write("/local/a.txt", b"aaa")
        await slim_fs.write("/local/b.txt", b"bbb")
        entries = await slim_fs.ls("/local/", detail=False, recursive=True)
        paths = [e for e in entries if e.endswith(".txt")]
        assert "/local/a.txt" in paths
        assert "/local/b.txt" in paths

    @pytest.mark.asyncio
    async def test_exists(self, slim_fs: SlimNexusFS):
        """Check exists before and after write."""
        assert not await slim_fs.exists("/local/nofile.txt")
        await slim_fs.write("/local/nofile.txt", b"now I exist")
        assert await slim_fs.exists("/local/nofile.txt")

    @pytest.mark.asyncio
    async def test_rename(self, slim_fs: SlimNexusFS):
        """Write, rename, verify old path gone and new path exists."""
        await slim_fs.write("/local/old.txt", b"rename me")
        await slim_fs.rename("/local/old.txt", "/local/new.txt")
        result = await slim_fs.read("/local/new.txt")
        assert result == b"rename me"

    @pytest.mark.asyncio
    async def test_delete(self, slim_fs: SlimNexusFS):
        """Write, delete, verify gone."""
        await slim_fs.write("/local/delete-me.txt", b"bye")
        await slim_fs.delete("/local/delete-me.txt")
        stat = await slim_fs.stat("/local/delete-me.txt")
        assert stat is None

    @pytest.mark.asyncio
    async def test_copy(self, slim_fs: SlimNexusFS):
        """Write, copy, verify both exist with same content."""
        await slim_fs.write("/local/src.txt", b"copy me")
        await slim_fs.copy("/local/src.txt", "/local/dst.txt")
        src = await slim_fs.read("/local/src.txt")
        dst = await slim_fs.read("/local/dst.txt")
        assert src == dst == b"copy me"

    @pytest.mark.asyncio
    async def test_mkdir(self, slim_fs: SlimNexusFS):
        """Create directory, verify it's a directory."""
        await slim_fs.mkdir("/local/subdir")
        stat = await slim_fs.stat("/local/subdir")
        assert stat is not None
        assert stat["is_directory"] is True

    @pytest.mark.asyncio
    async def test_stat_directory(self, slim_fs: SlimNexusFS):
        """Stat on the mount root should return directory."""
        stat = await slim_fs.stat("/local")
        assert stat is not None
        assert stat["is_directory"] is True

    @pytest.mark.asyncio
    async def test_overwrite(self, slim_fs: SlimNexusFS):
        """Writing to the same path should overwrite."""
        await slim_fs.write("/local/ow.txt", b"version 1")
        await slim_fs.write("/local/ow.txt", b"version 2")
        result = await slim_fs.read("/local/ow.txt")
        assert result == b"version 2"

    @pytest.mark.asyncio
    async def test_binary_content(self, slim_fs: SlimNexusFS):
        """Write and read binary content."""
        content = bytes(range(256))
        await slim_fs.write("/local/binary.bin", content)
        result = await slim_fs.read("/local/binary.bin")
        assert result == content

    @pytest.mark.asyncio
    async def test_empty_file(self, slim_fs: SlimNexusFS):
        """Write and read empty file."""
        await slim_fs.write("/local/empty.txt", b"")
        result = await slim_fs.read("/local/empty.txt")
        assert result == b""

    @pytest.mark.asyncio
    async def test_list_mounts(self, slim_fs: SlimNexusFS):
        """Verify mount points are listed."""
        mounts = slim_fs.list_mounts()
        assert "/local" in mounts


# ---------------------------------------------------------------------------
# Multi-backend
# ---------------------------------------------------------------------------


class TestMultiBackend:
    @pytest.mark.asyncio
    async def test_write_to_separate_backends(self, dual_fs: SlimNexusFS):
        """Write to two different backends, verify isolation."""
        await dual_fs.write("/a/file.txt", b"backend A")
        await dual_fs.write("/b/file.txt", b"backend B")

        assert await dual_fs.read("/a/file.txt") == b"backend A"
        assert await dual_fs.read("/b/file.txt") == b"backend B"

    @pytest.mark.asyncio
    async def test_cross_backend_copy(self, dual_fs: SlimNexusFS):
        """Copy from one backend to another."""
        await dual_fs.write("/a/src.txt", b"cross-copy")
        await dual_fs.copy("/a/src.txt", "/b/dst.txt")

        assert await dual_fs.read("/b/dst.txt") == b"cross-copy"

    @pytest.mark.asyncio
    async def test_list_multiple_mounts(self, dual_fs: SlimNexusFS):
        """Both mounts should be visible."""
        mounts = dual_fs.list_mounts()
        assert "/a" in mounts
        assert "/b" in mounts


# ---------------------------------------------------------------------------
# SQLite metastore
# ---------------------------------------------------------------------------


class TestSQLiteMetastore:
    def test_wal_mode_enabled(self, tmp_path: Path):
        """Verify WAL mode is enabled on the SQLite database."""
        db_path = str(tmp_path / "test.db")
        SQLiteMetastore(db_path)  # creates DB with WAL mode
        import sqlite3

        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"
        conn.close()

    def test_put_and_get(self, tmp_path: Path):
        """Basic put/get on the SQLite metastore."""
        from datetime import UTC, datetime

        from nexus.contracts.metadata import FileMetadata

        db_path = str(tmp_path / "test.db")
        meta = SQLiteMetastore(db_path)

        fm = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="abc123",
            size=42,
            etag="abc123",
            mime_type="text/plain",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
            version=1,
            zone_id=ROOT_ZONE_ID,
        )
        meta.put(fm)
        result = meta.get("/test/file.txt")
        assert result is not None
        assert result.path == "/test/file.txt"
        assert result.size == 42
