"""Nexus CLI MCP Commands - Model Context Protocol server.

This module contains MCP-related CLI commands for:
- Starting MCP server with stdio transport (for Claude Desktop, etc.)
- Starting MCP server with HTTP transport (for web clients)
"""

import sys
from typing import TYPE_CHECKING, Any, cast

import click

from nexus.cli.utils import (
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


def _add_health_check_route(mcp_server: Any) -> None:
    """Add health check route for HTTP transports.

    This adds a GET /health endpoint for Docker health checks.

    Args:
        mcp_server: FastMCP server instance
    """
    try:
        from starlette.responses import JSONResponse

        @mcp_server.custom_route("/health", methods=["GET"])
        async def health_check(_request: Any) -> Any:
            """Health check endpoint for Docker and monitoring."""
            return JSONResponse({"status": "healthy", "service": "nexus-mcp"})

        console.print("[nexus.success]✓ Health check endpoint enabled (/health)[/nexus.success]")

    except Exception as e:
        console.print(
            f"[nexus.warning]Warning: Failed to add health check route: {e}[/nexus.warning]"
        )


def _add_api_key_middleware(mcp_server: Any) -> None:
    """Add HTTP middleware to extract API keys from headers.

    This middleware extracts the API key from the X-Nexus-API-Key header
    and sets it in the request context for use by MCP tools.

    Args:
        mcp_server: FastMCP server instance
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware

        from nexus.bricks.mcp import reset_request_api_key, set_request_api_key

        class APIKeyMiddleware(BaseHTTPMiddleware):
            """Middleware to extract API key from HTTP headers."""

            async def dispatch(self, request: "Request", call_next: Any) -> "Response":
                # Extract API key from header (try both formats)
                api_key = request.headers.get("X-Nexus-API-Key") or request.headers.get(
                    "x-nexus-api-key"
                )

                # Also support Authorization header format: "Bearer <api-key>"
                if not api_key:
                    auth_header = request.headers.get("Authorization") or request.headers.get(
                        "authorization"
                    )
                    if auth_header and auth_header.startswith("Bearer "):
                        api_key = auth_header[7:]  # Remove "Bearer " prefix

                # Set API key in context if present
                token = None
                if api_key:
                    token = set_request_api_key(api_key)

                try:
                    # Process request
                    response = await call_next(request)
                    return cast("Response", response)
                finally:
                    # Clean up context
                    if token is not None:
                        reset_request_api_key(token)

        # Add middleware to the underlying Starlette app
        # FastMCP's http_app is a method that returns the Starlette application
        if hasattr(mcp_server, "http_app"):
            app = mcp_server.http_app()
            app.add_middleware(APIKeyMiddleware)
            console.print(
                "[nexus.success]✓ API key middleware enabled (X-Nexus-API-Key header)[/nexus.success]"
            )
        else:
            console.print(
                "[nexus.warning]Warning: http_app not available, middleware not added[/nexus.warning]"
            )

    except Exception as e:
        console.print(
            f"[nexus.warning]Warning: Failed to add API key middleware: {e}[/nexus.warning]"
        )


def _add_audit_log_middleware(mcp_server: Any) -> None:
    """Add MCP audit-log middleware (Issue #3779).

    Emits structured JSON per HTTP request to stdout and publishes
    the same payload to the Redis ``nexus:audit:mcp`` channel.
    """
    try:
        from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware

        if hasattr(mcp_server, "http_app"):
            app = mcp_server.http_app()
            app.add_middleware(MCPAuditLogMiddleware)
            console.print(
                "[nexus.success]✓ Audit log middleware enabled (nexus:audit:mcp)[/nexus.success]"
            )
    except Exception as e:
        console.print(
            f"[nexus.warning]Warning: Failed to add audit log middleware: {e}[/nexus.warning]"
        )


def _add_rate_limit_middleware(mcp_server: Any) -> None:
    """Install SlowAPI-based rate-limit middleware (Issue #3779).

    Per-token tiers keyed by the same header conventions as the
    HTTP API. Enabled via ``MCP_RATE_LIMIT_ENABLED=true``.
    """
    try:
        from nexus.bricks.mcp.middleware_ratelimit import install_rate_limit

        if hasattr(mcp_server, "http_app"):
            app = mcp_server.http_app()
            install_rate_limit(app)
            console.print(
                "[nexus.success]✓ Rate-limit middleware installed (MCP_RATE_LIMIT_ENABLED to enforce)[/nexus.success]"
            )
    except Exception as e:
        console.print(
            f"[nexus.warning]Warning: Failed to add rate-limit middleware: {e}[/nexus.warning]"
        )


@click.group(name="mcp")
def mcp() -> None:
    """Model Context Protocol (MCP) server commands.

    Start MCP server to expose Nexus functionality to AI agents and tools.

    Examples:
        # Start server for Claude Desktop (stdio transport)
        nexus mcp serve --transport stdio

        # Start server for web clients (HTTP transport)
        nexus mcp serve --transport http --port 8081

    Configuration for Claude Desktop (~/.config/claude/claude_desktop_config.json):
        {
            "mcpServers": {
                "nexus": {
                    "command": "nexus",
                    "args": ["mcp", "serve", "--transport", "stdio"],
                    "env": {
                        "NEXUS_DATA_DIR": "/path/to/nexus-data"
                    }
                }
            }
        }

    For remote server with authentication:
        {
            "mcpServers": {
                "nexus": {
                    "command": "nexus",
                    "args": ["mcp", "serve", "--transport", "stdio"],
                    "env": {
                        "NEXUS_URL": "http://localhost:2026",
                        "NEXUS_API_KEY": "your-api-key-here"
                    }
                }
            }
        }
    """
    pass


@mcp.command(name="serve")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "sse"]),
    default="stdio",
    help="Transport type (stdio for Claude Desktop, http/sse for web clients)",
    show_default=True,
)
@click.option(
    "--host",
    default="0.0.0.0",
    help="Server host (only for http/sse transport)",
    show_default=True,
)
@click.option(
    "--port",
    default=8081,
    type=int,
    help="Server port (only for http/sse transport)",
    show_default=True,
)
@click.option(
    "--api-key",
    help="API key for remote server authentication (or set NEXUS_API_KEY env var)",
    envvar="NEXUS_API_KEY",
)
@add_backend_options
def serve(
    transport: str,
    host: str,
    port: int,
    api_key: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Start Nexus MCP server.

    Exposes Nexus functionality through the Model Context Protocol,
    allowing AI agents and tools to interact with your Nexus filesystem.

    Available Tools:
    - nexus_read_file: Read file content
    - nexus_write_file: Write content to file
    - nexus_delete_file: Delete a file
    - nexus_list_files: List directory contents
    - nexus_file_info: Get file metadata
    - nexus_mkdir: Create directory
    - nexus_rmdir: Remove directory
    - nexus_glob: Search files by pattern
    - nexus_grep: Search file contents
    - nexus_semantic_search: Natural language search
    - nexus_store_memory: Store agent memory
    - nexus_query_memory: Query agent memories
    - nexus_list_workflows: List available workflows
    - nexus_execute_workflow: Execute a workflow

    Resources:
    - nexus://files/{path}: Browse files as resources

    Prompts:
    - file_analysis_prompt: Analyze a file
    - search_and_summarize_prompt: Search and summarize content

    Examples:
        # Start for Claude Desktop (stdio transport)
        nexus mcp serve --transport stdio

        # Start for web clients (HTTP transport)
        nexus mcp serve --transport http --port 8081

        # Use with remote Nexus server
        NEXUS_URL=http://localhost:2026 nexus mcp serve

        # Use with remote Nexus server and API key
        nexus mcp serve --url http://localhost:2026 --api-key YOUR_KEY
        # Or via environment:
        NEXUS_URL=http://localhost:2026 NEXUS_API_KEY=YOUR_KEY nexus mcp serve
    """
    import asyncio

    asyncio.run(_async_serve(transport, host, port, api_key, remote_url, remote_api_key))


async def _async_serve(
    transport: str,
    host: str,
    port: int,
    api_key: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        # Check if fastmcp is installed
        try:
            from nexus.bricks.mcp import create_mcp_server
        except ImportError:
            # For stdio mode, print errors to stderr
            import sys as sys_module

            print(
                "Error: MCP support not available. "
                "Install with: pip install 'nexus-ai-fs' (fastmcp should be included)",
                file=sys_module.stderr,
            )
            sys.exit(1)

        # For stdio transport, suppress all startup messages (they interfere with JSON-RPC)
        # These messages should go to stderr, not stdout
        is_stdio = transport == "stdio"

        def log_msg(msg: str) -> None:
            """Print message to stderr for stdio mode, console otherwise."""
            if is_stdio:
                print(msg, file=sys.stderr)
            else:
                console.print(msg)

        # Get filesystem instance
        log_msg("Initializing Nexus MCP server...")

        log_msg(f"  Remote URL: {remote_url}")
        if api_key:
            log_msg(f"  API Key: {'*' * 8}")
        nx = await get_filesystem(remote_url, remote_api_key)

        log_msg(f"  Transport: {transport}")

        if transport in ["http", "sse"]:
            log_msg(f"  Host: {host}")
            log_msg(f"  Port: {port}")

        # Only show verbose info for non-stdio transports
        if not is_stdio:
            console.print()

            # Display available tools
            console.print("[bold nexus.value]Available Tools:[/bold nexus.value]")
            tools = [
                "nexus_read_file",
                "nexus_write_file",
                "nexus_delete_file",
                "nexus_list_files",
                "nexus_file_info",
                "nexus_mkdir",
                "nexus_rmdir",
                "nexus_glob",
                "nexus_grep",
                "nexus_semantic_search",
                "nexus_resolve_context",
                "nexus_store_memory",
                "nexus_query_memory",
                "nexus_list_workflows",
                "nexus_execute_workflow",
            ]
            for tool in tools:
                console.print(f"  • [nexus.value]{tool}[/nexus.value]")

            console.print()
            console.print("[bold nexus.value]Resources:[/bold nexus.value]")
            console.print("  • [nexus.path]nexus://files/{{path}}[/nexus.path] - Browse files")

            console.print()
            console.print("[bold nexus.value]Prompts:[/bold nexus.value]")
            console.print("  • [nexus.value]file_analysis_prompt[/nexus.value] - Analyze a file")
            console.print(
                "  • [nexus.value]search_and_summarize_prompt[/nexus.value] - Search and summarize"
            )

            console.print()
            console.print(
                f"[nexus.warning]Starting HTTP server on http://{host}:{port}[/nexus.warning]"
            )
            console.print()

            console.print("[nexus.success]Starting MCP server...[/nexus.success]")
            console.print("[nexus.warning]Press Ctrl+C to stop[/nexus.warning]")
            console.print()

        # Build manifest resolver callable if available (Issue #2984)
        manifest_resolve_fn = None
        if nx is not None:
            _raw_resolver = getattr(nx, "manifest_resolver", None)
            if _raw_resolver is not None:
                try:
                    from nexus.factory.manifest_adapter import build_manifest_resolve_fn

                    manifest_resolve_fn = build_manifest_resolve_fn(_raw_resolver, nx)
                except Exception:
                    pass  # Graceful degradation — tool returns "unavailable"

        # Create and run MCP server
        mcp_server = await create_mcp_server(
            nx=nx,
            remote_url=remote_url,
            api_key=api_key,
            manifest_resolver=manifest_resolve_fn,
        )

        # Add HTTP middleware and routes (for http/sse transports)
        if transport in ["http", "sse"]:
            # Middleware order (outermost to innermost, added in reverse):
            #   APIKey (innermost) → AuditLog → RateLimit (outermost).
            _add_health_check_route(mcp_server)
            _add_api_key_middleware(mcp_server)
            _add_audit_log_middleware(mcp_server)
            _add_rate_limit_middleware(mcp_server)

        # Run with appropriate transport
        if transport == "stdio":
            mcp_server.run(transport="stdio")
        elif transport == "http":
            mcp_server.run(transport="http", host=host, port=port)
        elif transport == "sse":
            mcp_server.run(transport="sse", host=host, port=port)

    except KeyboardInterrupt:
        console.print("\n[nexus.warning]MCP server stopped by user[/nexus.warning]")
    except Exception as e:
        handle_error(e)


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
