from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nexus.bricks.mcp.server import create_mcp_server, reset_request_api_key, set_request_api_key


def _get_tool(server, tool_name: str):
    if hasattr(server, "_local_provider"):
        for key, component in server._local_provider._components.items():
            if key.startswith("tool:") and component.name == tool_name:
                return component
    return server._tool_manager._tools[tool_name]


@pytest.mark.asyncio
async def test_hub_admin_tool_forwards_to_remote_rpc():
    calls = []

    def fake_call_rpc(method, params):
        calls.append((method, params))
        return {"tokens": [{"key_id": "nk_1", "name": "admin"}]}

    nx = SimpleNamespace(
        subject_id="admin",
        zone_id="root",
        is_admin=True,
        _nexus_remote_call_rpc=fake_call_rpc,
    )
    server = await create_mcp_server(nx=nx)
    tool = _get_tool(server, "nexus_hub_token_list")

    result = await tool.fn(show_revoked=True)

    assert calls == [("hub_admin_token_list", {"show_revoked": True})]
    assert json.loads(result)["tokens"][0]["key_id"] == "nk_1"


@pytest.mark.asyncio
async def test_hub_admin_tool_rejects_non_admin_before_rpc():
    calls = []

    def fake_call_rpc(method, params):
        calls.append((method, params))
        return {"tokens": []}

    nx = SimpleNamespace(
        subject_id="alice",
        zone_id="root",
        is_admin=False,
        _nexus_remote_call_rpc=fake_call_rpc,
    )
    server = await create_mcp_server(nx=nx)
    tool = _get_tool(server, "nexus_hub_token_list")

    result = await tool.fn(show_revoked=False)

    assert calls == []
    assert result.startswith("Error:")
    assert "Admin privileges required" in result


@pytest.mark.asyncio
async def test_hub_admin_tool_delegates_admin_check_for_per_request_remote_connection():
    calls = []

    def fake_call_rpc(method, params):
        calls.append((method, params))
        return {"tokens": []}

    nx = SimpleNamespace(
        _init_cred=SimpleNamespace(user_id="remote", is_admin=False),
        _nexus_remote_call_rpc=fake_call_rpc,
    )
    server = await create_mcp_server(nx=nx)
    tool = _get_tool(server, "nexus_hub_token_list")

    token = set_request_api_key("sk-admin")
    try:
        result = await tool.fn(show_revoked=False)
    finally:
        reset_request_api_key(token)

    assert calls == [("hub_admin_token_list", {"show_revoked": False})]
    assert json.loads(result) == {"tokens": []}
