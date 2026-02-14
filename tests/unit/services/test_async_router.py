"""Tests for AsyncVFSRouter wrapper (Issue #1440)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath, VFSRouterProtocol
from nexus.core.router import MountConfig, PathNotMountedError, RouteResult
from nexus.services.routing.async_router import AsyncVFSRouter, _to_mount_info, _to_resolved_path
from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_inner() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def wrapper(mock_inner: MagicMock) -> AsyncVFSRouter:
    return AsyncVFSRouter(mock_inner)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestConformance:
    def test_assert_protocol_conformance(self) -> None:
        assert_protocol_conformance(AsyncVFSRouter, VFSRouterProtocol)

    def test_isinstance_check(self, wrapper: AsyncVFSRouter) -> None:
        assert isinstance(wrapper, VFSRouterProtocol)


# ---------------------------------------------------------------------------
# Type conversion tests
# ---------------------------------------------------------------------------


class TestToResolvedPath:
    def test_basic_conversion(self) -> None:
        result = RouteResult(
            backend=MagicMock(),
            backend_path="project/file.txt",
            mount_point="/workspace",
            readonly=False,
        )
        resolved = _to_resolved_path(result, "/workspace/project/file.txt", "zone-1")
        assert isinstance(resolved, ResolvedPath)
        assert resolved.virtual_path == "/workspace/project/file.txt"
        assert resolved.backend_path == "project/file.txt"
        assert resolved.mount_point == "/workspace"
        assert resolved.readonly is False
        assert resolved.zone_id == "zone-1"

    def test_none_zone(self) -> None:
        result = RouteResult(
            backend=MagicMock(),
            backend_path="f.txt",
            mount_point="/",
            readonly=True,
        )
        resolved = _to_resolved_path(result, "/f.txt", None)
        assert resolved.zone_id is None
        assert resolved.readonly is True


class TestToMountInfo:
    def test_basic_conversion(self) -> None:
        config = MountConfig(
            mount_point="/workspace",
            backend=MagicMock(),
            priority=10,
            readonly=False,
        )
        info = _to_mount_info(config)
        assert isinstance(info, MountInfo)
        assert info.mount_point == "/workspace"
        assert info.priority == 10
        assert info.readonly is False

    def test_readonly_mount(self) -> None:
        config = MountConfig(
            mount_point="/archives",
            backend=MagicMock(),
            priority=0,
            readonly=True,
        )
        info = _to_mount_info(config)
        assert info.readonly is True


# ---------------------------------------------------------------------------
# Async method delegation (direct calls, no to_thread)
# ---------------------------------------------------------------------------


class TestRoute:
    @pytest.mark.asyncio()
    async def test_delegates_and_converts(
        self, wrapper: AsyncVFSRouter, mock_inner: MagicMock
    ) -> None:
        mock_inner.route.return_value = RouteResult(
            backend=MagicMock(),
            backend_path="file.txt",
            mount_point="/workspace",
            readonly=False,
        )
        resolved = await wrapper.route("/workspace/file.txt", zone_id="z1", is_admin=True)
        mock_inner.route.assert_called_once_with(
            "/workspace/file.txt",
            zone_id="z1",
            is_admin=True,
            check_write=False,
        )
        assert isinstance(resolved, ResolvedPath)
        assert resolved.zone_id == "z1"

    @pytest.mark.asyncio()
    async def test_propagates_not_mounted(
        self, wrapper: AsyncVFSRouter, mock_inner: MagicMock
    ) -> None:
        mock_inner.route.side_effect = PathNotMountedError("No mount for /unknown")
        with pytest.raises(PathNotMountedError):
            await wrapper.route("/unknown")


class TestAddMount:
    @pytest.mark.asyncio()
    async def test_delegates(self, wrapper: AsyncVFSRouter, mock_inner: MagicMock) -> None:
        backend = MagicMock()
        await wrapper.add_mount("/data", backend, priority=5, readonly=True)
        mock_inner.add_mount.assert_called_once_with(
            "/data",
            backend,
            priority=5,
            readonly=True,
        )


class TestRemoveMount:
    @pytest.mark.asyncio()
    async def test_returns_true(self, wrapper: AsyncVFSRouter, mock_inner: MagicMock) -> None:
        mock_inner.remove_mount.return_value = True
        assert await wrapper.remove_mount("/workspace") is True

    @pytest.mark.asyncio()
    async def test_returns_false(self, wrapper: AsyncVFSRouter, mock_inner: MagicMock) -> None:
        mock_inner.remove_mount.return_value = False
        assert await wrapper.remove_mount("/nonexistent") is False


class TestListMounts:
    @pytest.mark.asyncio()
    async def test_converts_list(self, wrapper: AsyncVFSRouter, mock_inner: MagicMock) -> None:
        mock_inner.list_mounts.return_value = [
            MountConfig(mount_point="/workspace", backend=MagicMock(), priority=10, readonly=False),
            MountConfig(mount_point="/shared", backend=MagicMock(), priority=5, readonly=True),
        ]
        result = await wrapper.list_mounts()
        assert len(result) == 2
        assert all(isinstance(m, MountInfo) for m in result)
        assert result[0].mount_point == "/workspace"
        assert result[1].readonly is True

    @pytest.mark.asyncio()
    async def test_empty_mounts(self, wrapper: AsyncVFSRouter, mock_inner: MagicMock) -> None:
        mock_inner.list_mounts.return_value = []
        assert await wrapper.list_mounts() == []
