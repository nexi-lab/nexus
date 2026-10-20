"""MCP tools over the configured native filesystem connection."""

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import click
from rich.table import Table
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nexus.bricks.mcp.credentials import http_api_key
from nexus.cli.utils import add_backend_options, console, get_filesystem
from nexus.lib.request_credentials import request_api_key
from nexus.remote.client import RemoteNexusFS

if TYPE_CHECKING:
    from nexus.bricks.mcp.profiles import ToolProfile, ToolProfileConfig


class _APIKeyMiddleware:
    """Require an explicit caller credential on the network transport."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = http_api_key(Headers(scope=scope))
        if not key and scope["path"] not in {"/health", "/metrics"}:
            response = JSONResponse(
                {"error": "missing_or_invalid_credential"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="nexus-mcp"'},
            )
            await response(scope, receive, send)
            return
        token = request_api_key.set(key)
        try:
            await self.app(scope, receive, send)
        finally:
            request_api_key.reset(token)


def _build_http_middleware() -> list[Any]:
    from starlette.middleware import Middleware

    from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware
    from nexus.bricks.mcp.middleware_ratelimit import build_rate_limit_middleware

    return [
        build_rate_limit_middleware(),
        Middleware(MCPAuditLogMiddleware),
        Middleware(_APIKeyMiddleware),
    ]


@click.group(name="mcp")
def mcp() -> None:
    """Expose native file operations, Search and tool discovery through MCP."""


class _ProfilePayload(TypedDict):
    name: str
    description: str | None
    extends: str | None
    tools: list[str]


def _default_tool_profiles_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "tool_profiles.yaml"


def _load_tool_profile_config(config_path: str | None) -> "ToolProfileConfig":
    from nexus.bricks.mcp.profiles import load_profiles

    return load_profiles(Path(config_path) if config_path else _default_tool_profiles_path())


def _profile_payload(profile: "ToolProfile") -> _ProfilePayload:
    return {
        "name": profile.name,
        "description": profile.description,
        "extends": profile.extends,
        "tools": sorted(profile.tools),
    }


def _print_json(payload: Any) -> None:
    import json

    click.echo(json.dumps(payload, indent=2, default=str))


@mcp.group(name="profile")
def mcp_profile() -> None:
    """Inspect configured MCP tool profiles."""
    pass


@mcp_profile.command(name="list")
@click.option("--config", "config_path", type=click.Path(), default=None)
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table")
def mcp_profile_list(config_path: str | None, output_format: str) -> None:
    """List configured MCP tool profiles."""
    config = _load_tool_profile_config(config_path)
    profiles = [_profile_payload(config.profiles[name]) for name in config.profile_names]
    payload = {
        "default_profile": config.default_profile,
        "profiles": profiles,
    }
    if output_format == "json":
        _print_json(payload)
        return

    table = Table(title="MCP Tool Profiles")
    table.add_column("Name", style="nexus.value")
    table.add_column("Extends", style="nexus.muted")
    table.add_column("Tools")
    table.add_column("Description")
    for item in profiles:
        table.add_row(
            item["name"],
            item["extends"] or "",
            str(len(item["tools"])),
            item["description"] or "",
        )
    console.print(table)


@mcp_profile.command(name="show")
@click.argument("profile_name", type=str)
@click.option("--config", "config_path", type=click.Path(), default=None)
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table")
def mcp_profile_show(profile_name: str, config_path: str | None, output_format: str) -> None:
    """Show one MCP tool profile."""
    config = _load_tool_profile_config(config_path)
    profile = config.get_profile(profile_name)
    if profile is None:
        raise click.ClickException(f"Unknown MCP tool profile '{profile_name}'")
    payload = _profile_payload(profile)
    if output_format == "json":
        _print_json(payload)
        return

    console.print(f"[bold nexus.value]{profile.name}[/bold nexus.value]")
    if profile.description:
        console.print(f"  Description: {profile.description}")
    if profile.extends:
        console.print(f"  Extends: [nexus.muted]{profile.extends}[/nexus.muted]")
    console.print("  Tools:")
    for tool in sorted(profile.tools):
        console.print(f"    - [nexus.value]{tool}[/nexus.value]")


@mcp.command()
@click.option(
    "--transport", type=click.Choice(["stdio", "http", "sse"]), default="stdio", show_default=True
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=click.IntRange(1, 65535), default=8081, show_default=True)
@click.option("--api-key", default=None, help="Credential for the stdio connection.")
@add_backend_options
def serve(
    transport: str,
    host: str,
    port: int,
    api_key: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Serve the native file and Search tools.

    stdio uses the configured connection credential or certificate. HTTP and
    SSE require a remote native owner and each client's explicit credential.

    Example: nexus mcp serve --transport http --remote-url grpc://localhost:2028
    """
    from nexus.bricks.mcp.server import create_mcp_server

    async def prepare() -> tuple[Any, Any]:
        nx = await get_filesystem(remote_url, remote_api_key or api_key)
        try:
            if transport != "stdio" and not isinstance(nx, RemoteNexusFS):
                raise click.ClickException("HTTP/SSE MCP requires a remote native filesystem")
            server = await create_mcp_server(nx=nx)
            if transport != "stdio":
                from nexus.bricks.mcp.metrics import install_metrics_route

                install_metrics_route(server)
            return nx, server
        except BaseException:
            nx.close()
            raise

    nx, server = asyncio.run(prepare())
    try:
        if transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(
                transport=transport, host=host, port=port, middleware=_build_http_middleware()
            )
    finally:
        nx.close()


@mcp.command(name="export-tools")
def export_tools_cmd() -> None:
    """Export CLI commands as MCP tool definitions (JSON Schema).

    Walks the Click command tree and outputs MCP-compatible tool
    definitions for every leaf command.  Each tool has a name,
    description, and inputSchema suitable for use with the Model
    Context Protocol.

    Examples:
        # Pretty-print to terminal
        nexus mcp export-tools

        # Pipe to file (auto-compact JSON)
        nexus mcp export-tools > tools.json

        # Filter with jq
        nexus mcp export-tools | jq '.[].name'
    """
    import json

    from nexus.cli.export_tools import walk_click_tree
    from nexus.cli.main import main as cli_root

    tools = walk_click_tree(cli_root, prefix="nexus")

    indent = 2 if sys.stdout.isatty() else None
    click.echo(json.dumps(tools, indent=indent, default=str))


def register_commands(cli: click.Group) -> None:
    """Register MCP commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mcp)
