"""Tests for AsyncNamespaceManager wrapper (Issue #1440)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.services.permissions.async_namespace_manager import (
    AsyncNamespaceManager,
    _to_namespace_mount,
)
from nexus.services.permissions.namespace_manager import MountEntry
from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol, NamespaceMount
from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_inner() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def wrapper(mock_inner: MagicMock) -> AsyncNamespaceManager:
    return AsyncNamespaceManager(mock_inner)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestConformance:
    def test_assert_protocol_conformance(self) -> None:
        assert_protocol_conformance(AsyncNamespaceManager, NamespaceManagerProtocol)

    def test_isinstance_check(self, wrapper: AsyncNamespaceManager) -> None:
        assert isinstance(wrapper, NamespaceManagerProtocol)


# ---------------------------------------------------------------------------
# MountEntry -> NamespaceMount conversion
# ---------------------------------------------------------------------------


class TestToNamespaceMount:
    def test_basic_conversion(self) -> None:
        entry = MountEntry(virtual_path="/workspace/project-alpha")
        mount = _to_namespace_mount(entry, ("user", "alice"), "zone-1")
        assert isinstance(mount, NamespaceMount)
        assert mount.virtual_path == "/workspace/project-alpha"
        assert mount.subject_type == "user"
        assert mount.subject_id == "alice"
        assert mount.zone_id == "zone-1"

    def test_none_zone(self) -> None:
        entry = MountEntry(virtual_path="/workspace")
        mount = _to_namespace_mount(entry, ("agent", "bot-1"), None)
        assert mount.zone_id is None
        assert mount.subject_type == "agent"


# ---------------------------------------------------------------------------
# Async method delegation
# ---------------------------------------------------------------------------


class TestIsVisible:
    @pytest.mark.asyncio()
    async def test_delegates_and_returns_bool(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        mock_inner.is_visible.return_value = True
        result = await wrapper.is_visible(("user", "alice"), "/workspace/file.txt", zone_id="z")
        mock_inner.is_visible.assert_called_once_with(
            ("user", "alice"),
            "/workspace/file.txt",
            zone_id="z",
        )
        assert result is True

    @pytest.mark.asyncio()
    async def test_invisible_path(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        mock_inner.is_visible.return_value = False
        result = await wrapper.is_visible(("user", "bob"), "/admin/secret")
        assert result is False


class TestGetMountTable:
    @pytest.mark.asyncio()
    async def test_converts_entries(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        mock_inner.get_mount_table.return_value = [
            MountEntry(virtual_path="/workspace/a"),
            MountEntry(virtual_path="/workspace/b"),
        ]
        result = await wrapper.get_mount_table(("user", "alice"), zone_id="z1")
        assert len(result) == 2
        assert all(isinstance(m, NamespaceMount) for m in result)
        assert result[0].virtual_path == "/workspace/a"
        assert result[0].subject_type == "user"
        assert result[0].subject_id == "alice"
        assert result[0].zone_id == "z1"

    @pytest.mark.asyncio()
    async def test_empty_mount_table(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        mock_inner.get_mount_table.return_value = []
        result = await wrapper.get_mount_table(("agent", "bot"))
        assert result == []


class TestInvalidate:
    @pytest.mark.asyncio()
    async def test_delegates(self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock) -> None:
        mock_inner.invalidate.return_value = None
        await wrapper.invalidate(("user", "alice"))
        mock_inner.invalidate.assert_called_once_with(("user", "alice"))


# ---------------------------------------------------------------------------
# Exception propagation through asyncio.to_thread
# ---------------------------------------------------------------------------


class TestExceptionPropagation:
    @pytest.mark.asyncio()
    async def test_is_visible_propagates_runtime_error(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        """Exceptions from is_visible propagate through to_thread."""
        mock_inner.is_visible.side_effect = RuntimeError("ReBAC rebuild failed")
        with pytest.raises(RuntimeError, match="ReBAC rebuild failed"):
            await wrapper.is_visible(("user", "alice"), "/workspace/file.txt")

    @pytest.mark.asyncio()
    async def test_get_mount_table_propagates_permission_error(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        """PermissionError from get_mount_table propagates through to_thread."""
        mock_inner.get_mount_table.side_effect = PermissionError("access denied")
        with pytest.raises(PermissionError, match="access denied"):
            await wrapper.get_mount_table(("user", "bob"), zone_id="z1")

    @pytest.mark.asyncio()
    async def test_invalidate_propagates_value_error(
        self, wrapper: AsyncNamespaceManager, mock_inner: MagicMock
    ) -> None:
        """ValueError from invalidate propagates through to_thread."""
        mock_inner.invalidate.side_effect = ValueError("invalid subject")
        with pytest.raises(ValueError, match="invalid subject"):
            await wrapper.invalidate(("user", "unknown"))
