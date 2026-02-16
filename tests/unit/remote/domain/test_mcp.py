"""Parametrized unit tests for MCPClient + AsyncMCPClient.

Issue #1603: Domain client tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.remote.domain.mcp import AsyncMCPClient, MCPClient

MCP_TEST_CASES = [
    ("list_mounts", {}, "mcp_list_mounts"),
    ("list_tools", {"name": "test"}, "mcp_list_tools"),
    ("mount", {"name": "test"}, "mcp_mount"),
    ("unmount", {"name": "test"}, "mcp_unmount"),
    ("sync", {"name": "test"}, "mcp_sync"),
    ("backfill_directory_index", {}, "backfill_directory_index"),
]


@pytest.mark.parametrize("method,kwargs,expected_rpc", MCP_TEST_CASES)
def test_sync_mcp_dispatch(method, kwargs, expected_rpc):
    mock_rpc = Mock(return_value={})
    client = MCPClient(mock_rpc)
    getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs,expected_rpc", MCP_TEST_CASES)
async def test_async_mcp_dispatch(method, kwargs, expected_rpc):
    mock_rpc = AsyncMock(return_value={})
    client = AsyncMCPClient(mock_rpc)
    await getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


def test_mount_includes_tier_default():
    """mount() should include the default tier."""
    mock_rpc = Mock(return_value={})
    client = MCPClient(mock_rpc)
    client.mount("test-mount")
    params = mock_rpc.call_args[0][1]
    assert params["name"] == "test-mount"
    assert params["tier"] == "system"
