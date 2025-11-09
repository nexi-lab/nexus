"""Nexus CLI MCP Commands - Model Context Protocol server.

This module contains MCP-related CLI commands for:
- Starting MCP server with stdio transport (for Claude Desktop, etc.)
- Starting MCP server with HTTP transport (for web clients)
"""

from __future__ import annotations

import sys

import click

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
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
                        "NEXUS_URL": "http://localhost:8080",
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
    backend_config: BackendConfig,
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
        NEXUS_URL=http://localhost:8080 nexus mcp serve

        # Use with remote Nexus server and API key
        nexus mcp serve --url http://localhost:8080 --api-key YOUR_KEY
        # Or via environment:
        NEXUS_URL=http://localhost:8080 NEXUS_API_KEY=YOUR_KEY nexus mcp serve

        # Use with local backend
        nexus mcp serve --data-dir ./my-data
    """
    try:
        # Check if fastmcp is installed
        try:
            from nexus.mcp import create_mcp_server
        except ImportError:
            console.print(
                "[red]Error:[/red] MCP support not available. "
                "Install with: pip install 'nexus-ai-fs' (fastmcp should be included)"
            )
            sys.exit(1)

        # Get filesystem instance
        console.print("[green]Initializing Nexus MCP server...[/green]")

        # Check if using remote URL
        if backend_config.remote_url:
            console.print(f"  Remote URL: [cyan]{backend_config.remote_url}[/cyan]")
            if api_key:
                console.print(f"  API Key: [cyan]{'*' * 8}[/cyan]")
            nx = None
            remote_url = backend_config.remote_url
        else:
            console.print(f"  Backend: [cyan]{backend_config.backend}[/cyan]")
            if backend_config.backend == "gcs":
                console.print(f"  GCS Bucket: [cyan]{backend_config.gcs_bucket}[/cyan]")
            else:
                console.print(f"  Data Dir: [cyan]{backend_config.data_dir}[/cyan]")
            nx = get_filesystem(backend_config)
            remote_url = None

        console.print(f"  Transport: [cyan]{transport}[/cyan]")

        if transport in ["http", "sse"]:
            console.print(f"  Host: [cyan]{host}[/cyan]")
            console.print(f"  Port: [cyan]{port}[/cyan]")

        console.print()

        # Display available tools
        console.print("[bold cyan]Available Tools:[/bold cyan]")
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
            "nexus_store_memory",
            "nexus_query_memory",
            "nexus_list_workflows",
            "nexus_execute_workflow",
        ]
        for tool in tools:
            console.print(f"  • [cyan]{tool}[/cyan]")

        console.print()
        console.print("[bold cyan]Resources:[/bold cyan]")
        console.print("  • [cyan]nexus://files/{{path}}[/cyan] - Browse files")

        console.print()
        console.print("[bold cyan]Prompts:[/bold cyan]")
        console.print("  • [cyan]file_analysis_prompt[/cyan] - Analyze a file")
        console.print("  • [cyan]search_and_summarize_prompt[/cyan] - Search and summarize")

        console.print()

        if transport == "stdio":
            console.print("[yellow]Running in stdio mode for Claude Desktop[/yellow]")
            console.print()
            console.print("[bold cyan]Claude Desktop Configuration:[/bold cyan]")
            console.print("Add to ~/.config/claude/claude_desktop_config.json:")
            console.print()
            console.print("{")
            console.print('  "mcpServers": {')
            console.print('    "nexus": {')
            console.print('      "command": "nexus",')
            console.print('      "args": ["mcp", "serve", "--transport", "stdio"],')
            console.print('      "env": {')
            if backend_config.remote_url:
                console.print(f'        "NEXUS_URL": "{backend_config.remote_url}"')
                if api_key:
                    console.print('        "NEXUS_API_KEY": "your-api-key-here"')
            else:
                console.print(f'        "NEXUS_DATA_DIR": "{backend_config.data_dir}"')
            console.print("      }")
            console.print("    }")
            console.print("  }")
            console.print("}")
            console.print()
        else:
            console.print(f"[yellow]Starting HTTP server on http://{host}:{port}[/yellow]")
            console.print()

        console.print("[green]Starting MCP server...[/green]")
        console.print("[yellow]Press Ctrl+C to stop[/yellow]")
        console.print()

        # Create and run MCP server
        mcp_server = create_mcp_server(nx=nx, remote_url=remote_url, api_key=api_key)

        # Run with appropriate transport
        if transport == "stdio":
            mcp_server.run(transport="stdio")
        elif transport == "http":
            mcp_server.run(transport="http", host=host, port=port)
        elif transport == "sse":
            mcp_server.run(transport="sse", host=host, port=port)

    except KeyboardInterrupt:
        console.print("\n[yellow]MCP server stopped by user[/yellow]")
    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register MCP commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mcp)
