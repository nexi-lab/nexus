"""Tests for MCPService (Issue #1287, Decision 9A).

Tests cover:
- Initialization with various dependency configurations
- _get_mcp_mount_manager: requires nexus_fs
- mcp_mount: validation errors
- RPC decorators on public methods
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.exceptions import ValidationError
from nexus.services.mcp_service import MCPService


class TestMCPServiceInit:
    """Test MCPService initialization."""

    def test_init_with_none(self):
        """Test service initialization with no dependencies."""
        service = MCPService(nexus_fs=None)
        assert service.nexus_fs is None

    def test_init_with_nexus_fs(self):
        """Test backward compatibility: nexus_fs accepted."""
        mock_fs = MagicMock()
        service = MCPService(nexus_fs=mock_fs)
        assert service.nexus_fs is mock_fs


class TestMCPServiceMountManager:
    """Test MCPService._get_mcp_mount_manager()."""

    def test_requires_nexus_fs(self):
        """Test that _get_mcp_mount_manager raises without nexus_fs."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            service._get_mcp_mount_manager()

    def test_returns_manager_when_nexus_fs_configured(self):
        """Test that mount manager is created when nexus_fs is configured."""
        mock_fs = MagicMock()
        service = MCPService(nexus_fs=mock_fs)

        # Should not raise when nexus_fs is configured
        result = service._get_mcp_mount_manager()
        assert result is not None


class TestMCPServiceMount:
    """Test MCPService.mcp_mount() validation."""

    @pytest.mark.asyncio
    async def test_mount_requires_command_or_url(self):
        """Test that mcp_mount rejects mount without command or url."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(ValidationError, match="Either command or url is required"):
            await service.mcp_mount(name="test")

    @pytest.mark.asyncio
    async def test_mount_requires_nexus_fs_for_execution(self):
        """Test that mcp_mount requires nexus_fs for execution."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            await service.mcp_mount(name="test", command="echo hello")


class TestMCPServiceListMounts:
    """Test MCPService.mcp_list_mounts()."""

    @pytest.mark.asyncio
    async def test_list_mounts_requires_nexus_fs(self):
        """Test mcp_list_mounts requires nexus_fs."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            await service.mcp_list_mounts()


class TestMCPServiceListTools:
    """Test MCPService.mcp_list_tools()."""

    @pytest.mark.asyncio
    async def test_list_tools_requires_nexus_fs(self):
        """Test mcp_list_tools requires nexus_fs."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            await service.mcp_list_tools(name="github")


class TestMCPServiceUnmount:
    """Test MCPService.mcp_unmount()."""

    @pytest.mark.asyncio
    async def test_unmount_requires_nexus_fs(self):
        """Test mcp_unmount requires nexus_fs."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            await service.mcp_unmount(name="github")


class TestMCPServiceSync:
    """Test MCPService.mcp_sync()."""

    @pytest.mark.asyncio
    async def test_sync_requires_nexus_fs(self):
        """Test mcp_sync requires nexus_fs."""
        service = MCPService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            await service.mcp_sync(name="github")


class TestMCPServiceRPCMethods:
    """Test that MCPService methods have @rpc_expose decorators."""

    def test_mcp_list_mounts_is_rpc_exposed(self):
        service = MCPService(nexus_fs=None)
        assert hasattr(service.mcp_list_mounts, "_rpc_exposed")

    def test_mcp_list_tools_is_rpc_exposed(self):
        service = MCPService(nexus_fs=None)
        assert hasattr(service.mcp_list_tools, "_rpc_exposed")

    def test_mcp_mount_is_rpc_exposed(self):
        service = MCPService(nexus_fs=None)
        assert hasattr(service.mcp_mount, "_rpc_exposed")

    def test_mcp_unmount_is_rpc_exposed(self):
        service = MCPService(nexus_fs=None)
        assert hasattr(service.mcp_unmount, "_rpc_exposed")

    def test_mcp_sync_is_rpc_exposed(self):
        service = MCPService(nexus_fs=None)
        assert hasattr(service.mcp_sync, "_rpc_exposed")
