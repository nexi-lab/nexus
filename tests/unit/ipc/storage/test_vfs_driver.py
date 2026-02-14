"""Unit tests for VFSStorageDriver.

Verifies that the VFS storage driver correctly delegates all operations
to the underlying VFSOperations instance.
"""

from __future__ import annotations

import pytest

from nexus.ipc.storage.vfs_driver import VFSStorageDriver
from tests.unit.ipc.fakes import InMemoryVFS

ZONE = "test-zone"


@pytest.fixture
def vfs() -> InMemoryVFS:
    return InMemoryVFS()


@pytest.fixture
def driver(vfs: InMemoryVFS) -> VFSStorageDriver:
    return VFSStorageDriver(vfs=vfs)


class TestVFSStorageDriver:
    """Tests for VFS storage driver delegation."""

    @pytest.mark.asyncio
    async def test_write_and_read(self, driver: VFSStorageDriver) -> None:
        await driver.write("/test/file.json", b'{"hello": "world"}', ZONE)
        data = await driver.read("/test/file.json", ZONE)
        assert data == b'{"hello": "world"}'

    @pytest.mark.asyncio
    async def test_read_nonexistent_raises(self, driver: VFSStorageDriver) -> None:
        with pytest.raises(FileNotFoundError):
            await driver.read("/nonexistent", ZONE)

    @pytest.mark.asyncio
    async def test_mkdir_and_list_dir(self, driver: VFSStorageDriver) -> None:
        await driver.mkdir("/test/dir", ZONE)
        await driver.write("/test/dir/a.json", b"a", ZONE)
        await driver.write("/test/dir/b.json", b"b", ZONE)
        entries = await driver.list_dir("/test/dir", ZONE)
        assert entries == ["a.json", "b.json"]

    @pytest.mark.asyncio
    async def test_list_dir_nonexistent_raises(self, driver: VFSStorageDriver) -> None:
        with pytest.raises(FileNotFoundError):
            await driver.list_dir("/nonexistent", ZONE)

    @pytest.mark.asyncio
    async def test_count_dir(self, driver: VFSStorageDriver) -> None:
        await driver.mkdir("/test/dir", ZONE)
        await driver.write("/test/dir/a.json", b"a", ZONE)
        await driver.write("/test/dir/b.json", b"b", ZONE)
        await driver.write("/test/dir/c.json", b"c", ZONE)
        count = await driver.count_dir("/test/dir", ZONE)
        assert count == 3

    @pytest.mark.asyncio
    async def test_count_dir_empty(self, driver: VFSStorageDriver) -> None:
        await driver.mkdir("/test/empty", ZONE)
        count = await driver.count_dir("/test/empty", ZONE)
        assert count == 0

    @pytest.mark.asyncio
    async def test_count_dir_nonexistent_raises(self, driver: VFSStorageDriver) -> None:
        with pytest.raises(FileNotFoundError):
            await driver.count_dir("/nonexistent", ZONE)

    @pytest.mark.asyncio
    async def test_rename(self, driver: VFSStorageDriver) -> None:
        await driver.write("/test/src.json", b"data", ZONE)
        await driver.rename("/test/src.json", "/test/dst.json", ZONE)
        data = await driver.read("/test/dst.json", ZONE)
        assert data == b"data"
        with pytest.raises(FileNotFoundError):
            await driver.read("/test/src.json", ZONE)

    @pytest.mark.asyncio
    async def test_rename_nonexistent_raises(self, driver: VFSStorageDriver) -> None:
        with pytest.raises(FileNotFoundError):
            await driver.rename("/nonexistent", "/dst", ZONE)

    @pytest.mark.asyncio
    async def test_exists(self, driver: VFSStorageDriver) -> None:
        assert not await driver.exists("/test/file.json", ZONE)
        await driver.write("/test/file.json", b"data", ZONE)
        assert await driver.exists("/test/file.json", ZONE)

    @pytest.mark.asyncio
    async def test_mkdir_idempotent(self, driver: VFSStorageDriver) -> None:
        await driver.mkdir("/test/dir", ZONE)
        await driver.mkdir("/test/dir", ZONE)  # Should not raise
        assert await driver.exists("/test/dir", ZONE)

    @pytest.mark.asyncio
    async def test_mkdir_creates_parents(self, driver: VFSStorageDriver) -> None:
        await driver.mkdir("/a/b/c", ZONE)
        assert await driver.exists("/a", ZONE)
        assert await driver.exists("/a/b", ZONE)
        assert await driver.exists("/a/b/c", ZONE)

    @pytest.mark.asyncio
    async def test_delegates_to_underlying_vfs(
        self, vfs: InMemoryVFS, driver: VFSStorageDriver
    ) -> None:
        """Verify driver writes are visible through the underlying VFS."""
        await driver.write("/direct/file.txt", b"content", ZONE)
        # Read directly from the VFS (not the driver) to verify delegation
        data = await vfs.read("/direct/file.txt", ZONE)
        assert data == b"content"
