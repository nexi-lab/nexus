"""Nexus CLI MCP Commands - Model Context Protocol server.

This module contains MCP-related CLI commands for:
- Starting MCP server with stdio transport (for Claude Desktop, etc.)
- Starting MCP server with HTTP transport (for web clients)
"""

import sys
from typing import TYPE_CHECKING, Any, cast

import click
from starlette.middleware.base import BaseHTTPMiddleware

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


def _extract_bearer_token(request: "Request") -> str | None:
    """Pull an API key from ``X-Nexus-API-Key`` or ``Authorization: Bearer``."""
    api_key = request.headers.get("X-Nexus-API-Key") or request.headers.get("x-nexus-api-key")
    if api_key:
        return api_key
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return None


class _APIKeyMiddleware(BaseHTTPMiddleware):
    """Extract API key from HTTP headers into request contextvar.

    Defined at module level so it can be passed to
    ``mcp.run(middleware=[Middleware(_APIKeyMiddleware)])``.
    FastMCP's ``http_app()`` returns a fresh Starlette app on each
    call, so middleware added after the fact is lost.

    Hub opt-in (#3784): when ``NEXUS_MCP_REQUIRE_BEARER=true``, missing
    bearer is rejected with 401 BEFORE the tool layer can fall back to
    an ambient ``_default_nx`` connection seeded with ``NEXUS_API_KEY``
    / profile credentials. This prevents unauthenticated requests from
    executing as the frontend's ambient identity on two-service hub
    deployments. ``/health`` is always allowed through so container
    healthchecks keep working.
    """

    async def dispatch(self, request: "Request", call_next: Any) -> "Response":
        import os

        from nexus.bricks.mcp import reset_request_api_key, set_request_api_key

        api_key = _extract_bearer_token(request)

        # Fail-closed when the deployment opts into hub-frontend mode.
        require_bearer = os.environ.get("NEXUS_MCP_REQUIRE_BEARER", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if require_bearer and not api_key and request.url.path != "/health":
            from starlette.responses import JSONResponse

            return cast(
                "Response",
                JSONResponse(
                    {"error": "missing_bearer_token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="nexus-hub"'},
                ),
            )

        token = set_request_api_key(api_key) if api_key else None
        try:
            response = await call_next(request)
            return cast("Response", response)
        finally:
            if token is not None:
                reset_request_api_key(token)


def _reject_embedded_hub_mode(transport: str, remote_url: str | None = None) -> None:
    """Refuse ``nexus mcp serve --transport http`` in embedded hub mode (#3784).

    "Embedded hub" = ``NEXUS_DATABASE_URL`` set AND no remote URL
    resolvable from ``--remote-url``, ``NEXUS_URL``, or the active
    profile, on an HTTP transport. This configuration would accept
    bearer tokens but run every tool call against the ambient local
    ``NexusFS`` — no per-token identity, no zone isolation, no
    connection to the hub's Postgres auth layer in the tool path.

    The supported hub deployment is the two-service design in
    ``docker-compose.hub.yml``: an ``nexusd`` RPC server plus a thin
    ``nexus mcp serve --url http://nexus:2026`` frontend. The frontend
    opens a per-request remote ``NexusFS`` with the client's bearer
    and the RPC server's ``DatabaseAPIKeyAuth`` enforces identity/zone
    on every call.

    We fail closed at startup so operators see a clear error instead of
    silently getting an unsafe deployment. Launches that provide the
    remote URL via any supported channel (``--remote-url`` CLI flag,
    ``NEXUS_URL`` env, active profile) are allowed through — those are
    the two-service-safe shapes we actually want people to use.
    """
    import os

    # stdio is single-user (no network) and safe. Every other transport
    # (http, sse, and any future network transport) accepts bearer
    # tokens from multiple clients, so the same guard applies (#3784
    # round 10: SSE was previously skipped here and silently fell back
    # to the ambient NexusFS).
    if transport == "stdio":
        return
    if not os.environ.get("NEXUS_DATABASE_URL"):
        return

    # Resolve the effective remote URL the same way `_async_serve` will
    # — CLI flag first, then env, then active profile — so a safe
    # remote frontend launched as
    # `nexus --profile hub mcp serve --transport http` is not
    # false-rejected. `get_filesystem` reads the profile from
    # ``ctx.obj['profile']`` (set by the root `nexus` command), so we
    # do the same here.
    if remote_url:
        return
    if os.environ.get("NEXUS_URL"):
        return
    try:
        from nexus.cli.config import resolve_connection

        profile_name = None
        try:
            ctx = click.get_current_context(silent=True)
            if ctx is not None and ctx.obj:
                profile_name = ctx.obj.get("profile")
        except RuntimeError:
            pass

        resolved = resolve_connection(
            remote_url=None,
            remote_api_key=None,
            profile_name=profile_name,
        )
        if getattr(resolved, "is_remote", False):
            return
    except Exception:
        # If profile resolution itself is broken, fall through to the
        # fail-closed error below — that's safer than masking a missing
        # remote URL with an unrelated exception.
        pass

    raise click.ClickException(
        "Embedded hub mode (NEXUS_DATABASE_URL set + no remote URL + "
        "--transport http) is not supported: tool calls would run under "
        "ambient local identity without per-token zone isolation. "
        "Use the two-service hub deployment instead: run `nexusd` as the "
        "RPC server and point this MCP frontend at it with "
        "`--remote-url http://<nexus-host>:2026` or NEXUS_URL=…. "
        "See docker-compose.hub.yml and docs/hub-deploy.md."
    )


def _build_http_middleware() -> list[Any]:
    """Build the Starlette middleware list for MCP HTTP transport (#3779).

    Order (outermost → innermost):
      1. RateLimit — short-circuit with 429 before any work
      2. AuditLog  — emit structured record per request
      3. APIKey    — set ``_request_api_key`` contextvar for tool handlers
    """
    from starlette.middleware import Middleware

    from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware
    from nexus.bricks.mcp.middleware_ratelimit import build_rate_limit_middleware

    items: list[Any] = []
    try:
        items.append(build_rate_limit_middleware())
    except Exception as e:
        console.print(
            f"[nexus.warning]Warning: Failed to build rate-limit middleware: {e}[/nexus.warning]"
        )
    items.append(Middleware(MCPAuditLogMiddleware))
    items.append(Middleware(_APIKeyMiddleware))
    console.print(
        "[nexus.success]✓ MCP HTTP middleware chain: APIKey → AuditLog → RateLimit[/nexus.success]"
    )
    return items


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
    import os

    # Resolve the effective remote URL/api_key once — same logic
    # ``get_filesystem`` uses — and thread the resolved values into
    # both the startup guard and the MCP server. For profile-only
    # launches (e.g. `nexus --profile hub mcp serve`), this promotes
    # the profile's URL into the `remote_url` passed to
    # `create_mcp_server`, so per-request bearer tokens actually build
    # per-request remote connections instead of falling through to an
    # ambient `_default_nx` (#3784 round 7 fix).
    resolved_remote_url = remote_url
    resolved_remote_api_key = remote_api_key
    try:
        from nexus.cli.config import resolve_connection

        profile_name = None
        try:
            ctx = click.get_current_context(silent=True)
            if ctx is not None and ctx.obj:
                profile_name = ctx.obj.get("profile")
        except RuntimeError:
            pass

        resolved = resolve_connection(
            remote_url=remote_url or os.environ.get("NEXUS_URL"),
            remote_api_key=remote_api_key or os.environ.get("NEXUS_API_KEY"),
            profile_name=profile_name,
        )
        if getattr(resolved, "is_remote", False):
            resolved_remote_url = resolved.url or resolved_remote_url
            resolved_remote_api_key = resolved.api_key or resolved_remote_api_key
    except Exception:
        # Fall through with the raw CLI-passed values — downstream
        # paths handle None/empty safely (stdio single-user mode).
        pass

    # Fail-closed: refuse unsupported embedded-hub configuration before
    # we open a port (#3784). Pass the resolved URL so a profile-only
    # remote frontend is not false-rejected.
    _reject_embedded_hub_mode(transport, remote_url=resolved_remote_url)

    # When transport=http resolves a remote URL AND an ambient api_key
    # (from --api-key, NEXUS_API_KEY, or a profile), the MCP server's
    # `_default_nx` connection is seeded with that key — so missing
    # bearer silently executes as the profile identity. Auto-promote
    # NEXUS_MCP_REQUIRE_BEARER=true for that shape so unauthenticated
    # requests hit a 401 at the middleware instead. Operators who
    # intentionally want the ambient-identity fallback (e.g. a trusted
    # single-tenant sidecar) can opt out with
    # NEXUS_MCP_ALLOW_AMBIENT_KEY=true (#3784 round 8).
    if (
        transport in ("http", "sse")
        and resolved_remote_url
        and resolved_remote_api_key
        and os.environ.get("NEXUS_MCP_ALLOW_AMBIENT_KEY", "").lower() not in ("1", "true", "yes")
        and os.environ.get("NEXUS_MCP_REQUIRE_BEARER", "").lower() not in ("1", "true", "yes")
    ):
        os.environ["NEXUS_MCP_REQUIRE_BEARER"] = "true"
        console.print(
            "[nexus.warning]⚠ Auto-enabled NEXUS_MCP_REQUIRE_BEARER=true — remote "
            "URL + ambient API key detected. Set NEXUS_MCP_ALLOW_AMBIENT_KEY=true to "
            "opt out.[/nexus.warning]"
        )

    # FastMCP's .run(transport="http") starts its own event loop via anyio.run
    # and cannot be called from inside an already-running asyncio loop. Split
    # async setup (connects, creates server, installs middleware) from the
    # synchronous transport run.
    mcp_server = asyncio.run(
        _async_serve(
            transport,
            host,
            port,
            api_key,
            resolved_remote_url,
            resolved_remote_api_key,
        )
    )
    if mcp_server is None:
        return
    if transport == "stdio":
        mcp_server.run(transport="stdio")
    elif transport in ("http", "sse"):
        # Middleware MUST be passed at run() time — FastMCP's http_app() returns
        # a new Starlette instance on every call, so post-hoc add_middleware
        # is lost (#3779 integration fix).
        mcp_server.run(
            transport=transport, host=host, port=port, middleware=_build_http_middleware()
        )


async def _async_serve(
    transport: str,
    host: str,
    port: int,
    api_key: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> Any:
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

        # Add custom HTTP routes (health check). Middleware is installed at
        # mcp.run() time in the outer ``serve()`` caller via
        # ``_build_http_middleware()`` — FastMCP's http_app() returns a fresh
        # Starlette instance per call, so post-hoc add_middleware is lost.
        if transport in ["http", "sse"]:
            _add_health_check_route(mcp_server)

        return mcp_server

    except KeyboardInterrupt:
        console.print("\n[nexus.warning]MCP server stopped by user[/nexus.warning]")
        return None
    except Exception as e:
        handle_error(e)
        return None


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
