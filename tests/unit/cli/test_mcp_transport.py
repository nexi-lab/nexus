"""MCP ingress and filesystem ownership at the public CLI boundary."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner
from fastmcp import Client
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.bricks.mcp.profiles import load_profiles
from nexus.bricks.mcp.server import create_mcp_server
from nexus.cli.commands import mcp as commands
from nexus.lib.request_credentials import request_api_key
from nexus.remote.client import RemoteNexusFS


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({}, 401),
        ({"Authorization": "Bearer "}, 401),
        ({"Authorization": "Basic invalid"}, 401),
        ({"Authorization": "Bearer caller"}, 200),
        ({"X-Nexus-API-Key": "caller"}, 200),
        ({"Authorization": "Bearer first", "X-Nexus-API-Key": "second"}, 401),
        ([("Authorization", "Bearer first"), ("Authorization", "Bearer second")], 401),
    ],
)
def test_http_caller_cannot_inherit_or_replace_the_host_credential(headers, expected):
    async def endpoint(_request):
        return JSONResponse({"credential": request_api_key.get()})

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])
    app.add_middleware(commands._APIKeyMiddleware)
    scope = request_api_key.set("host-credential")
    try:
        with TestClient(app) as client:
            response = client.post("/mcp", headers=headers)
        assert response.status_code == expected
        if expected == 200:
            assert response.json() == {"credential": "caller"}
        else:
            assert response.headers["WWW-Authenticate"].startswith("Bearer ")
        assert request_api_key.get() == "host-credential"
    finally:
        request_api_key.reset(scope)


@pytest.mark.parametrize("path", ["/health", "/metrics"])
def test_monitoring_endpoints_need_no_file_credential(path):
    async def endpoint(_request):
        return JSONResponse({"credential": request_api_key.get()})

    app = Starlette(routes=[Route(path, endpoint)])
    app.add_middleware(commands._APIKeyMiddleware)
    with TestClient(app) as client:
        assert client.get(path).json() == {"credential": ""}


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_network_transport_refuses_a_local_filesystem(monkeypatch, transport):
    nx = Mock()
    monkeypatch.setattr(commands, "get_filesystem", AsyncMock(return_value=nx))
    result = CliRunner().invoke(commands.mcp, ["serve", "--transport", transport])
    assert result.exit_code != 0
    assert "requires a remote native filesystem" in result.output
    nx.close.assert_called_once_with()


@pytest.mark.parametrize("failure", [None, "construction", "run"])
def test_cli_closes_its_connection_on_success_and_failure(monkeypatch, failure):
    import nexus.bricks.mcp.server as implementation

    nx = Mock()
    server = Mock()
    server.run.side_effect = RuntimeError("transport failed") if failure == "run" else None
    builder = AsyncMock(return_value=server)
    if failure == "construction":
        builder.side_effect = RuntimeError("construction failed")
    monkeypatch.setattr(commands, "get_filesystem", AsyncMock(return_value=nx))
    monkeypatch.setattr(implementation, "create_mcp_server", builder)
    result = CliRunner().invoke(commands.mcp, ["serve"])
    assert result.exit_code == (0 if failure is None else 1)
    nx.close.assert_called_once_with()
    if failure != "construction":
        server.run.assert_called_once_with(transport="stdio")


def test_network_transport_uses_the_owned_remote_connection(monkeypatch):
    import nexus.bricks.mcp.server as implementation

    transport = Mock()
    nx = RemoteNexusFS(transport)
    server = Mock()
    get_filesystem = AsyncMock(return_value=nx)
    builder = AsyncMock(return_value=server)
    monkeypatch.setattr(commands, "get_filesystem", get_filesystem)
    monkeypatch.setattr(implementation, "create_mcp_server", builder)
    result = CliRunner().invoke(
        commands.mcp,
        [
            "serve",
            "--transport",
            "http",
            "--remote-url",
            "grpc://owner:2028",
            "--api-key",
            "caller",
        ],
    )
    assert result.exit_code == 0, result.output
    get_filesystem.assert_awaited_once_with("grpc://owner:2028", "caller")
    builder.assert_awaited_once_with(nx=nx)
    assert server.run.call_args.kwargs["transport"] == "http"
    assert nx.closed
    transport.close.assert_called_once_with()


async def test_shipped_profiles_name_only_registered_native_tools():
    server = await create_mcp_server(nx=Mock())
    async with Client(server) as client:
        registered = {tool.name for tool in await client.list_tools()}
    config = load_profiles(commands._default_tool_profiles_path())
    assert config.get_profile("full").tools == registered
    for profile in config.profiles.values():
        assert profile.tools <= registered
    result = CliRunner().invoke(commands.mcp, ["profile", "list", "--format", "json"])
    assert result.exit_code == 0
    assert {profile["name"] for profile in json.loads(result.output)["profiles"]} == set(
        config.profiles
    )
