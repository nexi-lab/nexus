"""Tests for the MCP hub admin tool (#3872)."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from nexus.bricks.mcp.server import create_mcp_server
from tests.unit.bricks.mcp.test_mcp_server_tools import get_tool, tool_exists


@pytest.mark.asyncio
async def test_nexus_hub_admin_tool_registered():
    server = await create_mcp_server(nx=Mock())

    assert tool_exists(server, "nexus_hub_admin")


@pytest.mark.asyncio
async def test_nexus_hub_admin_list_delegates_to_remote_service():
    service = Mock()
    service.admin_hub_token_list.return_value = {"tokens": []}
    nx = Mock()
    nx.service.return_value = service
    server = await create_mcp_server(nx=nx)

    tool = get_tool(server, "nexus_hub_admin")
    result = await tool.fn(action="list_tokens", arguments={"show_revoked": True})

    assert json.loads(result) == {"tokens": []}
    service.admin_hub_token_list.assert_called_once_with(show_revoked=True)


@pytest.mark.asyncio
async def test_nexus_hub_admin_permission_error_has_403_status():
    from nexus.contracts.exceptions import NexusPermissionError

    service = Mock()
    service.admin_hub_token_list.side_effect = NexusPermissionError("Admin privileges required")
    nx = Mock()
    nx.service.return_value = service
    server = await create_mcp_server(nx=nx)

    tool = get_tool(server, "nexus_hub_admin")
    result = await tool.fn(action="list_tokens", arguments={})

    payload = json.loads(result)
    assert payload["error"]["status"] == 403
    assert "Admin privileges required" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_nexus_hub_admin_validation_error_has_400_status():
    from nexus.contracts.exceptions import ValidationError

    service = Mock()
    service.admin_hub_token_create.side_effect = ValidationError("zones must not be empty")
    nx = Mock()
    nx.service.return_value = service
    server = await create_mcp_server(nx=nx)

    tool = get_tool(server, "nexus_hub_admin")
    result = await tool.fn(action="create_token", arguments={})

    payload = json.loads(result)
    assert payload["error"]["status"] == 400
    assert "zones must not be empty" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_nexus_hub_admin_unknown_action_has_400_status():
    server = await create_mcp_server(nx=Mock())

    tool = get_tool(server, "nexus_hub_admin")
    result = await tool.fn(action="bogus", arguments={})

    payload = json.loads(result)
    assert payload["error"]["status"] == 400
    assert "unknown hub admin action" in payload["error"]["message"]
