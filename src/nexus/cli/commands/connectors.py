"""Nexus CLI Connector Commands.

Commands for discovering and inspecting available connectors:
- nexus connectors list - List all registered connectors
- nexus connectors info - Show connector details

Works with both local and remote Nexus instances.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


@click.group(name="connectors")
def connectors_group() -> None:
    """Discover and inspect available connectors.

    Connectors are backend types that can be mounted in Nexus.
    Use these commands to see what connectors are available
    and their configuration requirements.

    Works with both local and remote Nexus instances:
        # Local (direct registry access)
        nexus connectors list

        # Remote (via RPC to server)
        nexus connectors list --remote-url http://localhost:8080

    Examples:
        # List all connectors
        nexus connectors list

        # List only storage connectors
        nexus connectors list --category storage

        # Show details for a specific connector
        nexus connectors info gcs_connector
    """
    pass


def _list_connectors_local(category: str | None) -> list[dict]:
    """List connectors from local registry."""
    from nexus.backends import ConnectorRegistry

    if category:
        connectors = ConnectorRegistry.list_by_category(category)
    else:
        connectors = ConnectorRegistry.list_all()

    return [
        {
            "name": c.name,
            "description": c.description,
            "category": c.category,
            "requires": c.requires,
            "user_scoped": c.user_scoped,
        }
        for c in connectors
    ]


def _list_connectors_remote(nx: Any, category: str | None) -> list[dict[str, Any]]:
    """List connectors from remote server via RPC."""
    result: list[dict[str, Any]] = nx.list_connectors(category=category)
    return result


@connectors_group.command(name="list")
@click.option(
    "--category",
    "-c",
    type=str,
    default=None,
    help="Filter by category (storage, api, oauth, database)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@add_backend_options
def list_connectors(category: str | None, as_json: bool, backend_config: BackendConfig) -> None:
    """List all registered connectors.

    Shows all available connector types that can be used with 'nexus mounts add'.

    Examples:
        # List all connectors (local)
        nexus connectors list

        # List connectors from remote server
        nexus connectors list --remote-url http://localhost:8080

        # List only storage connectors
        nexus connectors list --category storage

        # Output as JSON
        nexus connectors list --json
    """
    try:
        # Check if we're connecting to a remote server
        if backend_config.remote_url:
            nx = get_filesystem(backend_config)
            try:
                connectors = _list_connectors_remote(nx, category)
            except AttributeError:
                console.print("[red]Error:[/red] Server doesn't support list_connectors")
                console.print("[yellow]Hint:[/yellow] Update server to latest Nexus version")
                sys.exit(1)
        else:
            # Local mode - use registry directly
            connectors = _list_connectors_local(category)

        if not connectors:
            if category:
                console.print(f"[yellow]No connectors found in category '{category}'[/yellow]")
            else:
                console.print("[yellow]No connectors registered[/yellow]")
            return

        if as_json:
            console.print(json.dumps(connectors, indent=2))
            return

        # Create table
        table = Table(title="Available Connectors", show_header=True, header_style="bold cyan")
        table.add_column("Name", style="green")
        table.add_column("Description")
        table.add_column("Category", style="yellow")
        table.add_column("Dependencies", style="dim")

        for c in connectors:
            deps = ", ".join(c["requires"]) if c.get("requires") else "-"
            table.add_row(
                c["name"],
                c.get("description", ""),
                c.get("category", ""),
                deps,
            )

        console.print(table)
        console.print(f"\n[dim]Total: {len(connectors)} connectors[/dim]")

    except Exception as e:
        handle_error(e)


@connectors_group.command(name="info")
@click.argument("connector_name", type=str)
@add_backend_options
def connector_info(connector_name: str, backend_config: BackendConfig) -> None:
    """Show details for a specific connector.

    CONNECTOR_NAME: The connector identifier (e.g., gcs_connector, s3_connector)

    Examples:
        # Local
        nexus connectors info gcs_connector

        # Remote
        nexus connectors info gcs_connector --remote-url http://localhost:8080
    """
    try:
        # Get connector info
        if backend_config.remote_url:
            nx = get_filesystem(backend_config)
            try:
                connectors = _list_connectors_remote(nx, None)
                info = next((c for c in connectors if c["name"] == connector_name), None)
                if not info:
                    available = ", ".join(c["name"] for c in connectors)
                    console.print(f"[red]Unknown connector: {connector_name}[/red]")
                    console.print(f"[dim]Available: {available}[/dim]")
                    sys.exit(1)
            except AttributeError:
                console.print("[red]Error:[/red] Server doesn't support list_connectors")
                sys.exit(1)
        else:
            # Local mode
            from nexus.backends import ConnectorRegistry

            try:
                c = ConnectorRegistry.get_info(connector_name)
                info = {
                    "name": c.name,
                    "description": c.description,
                    "category": c.category,
                    "requires": c.requires,
                    "user_scoped": c.user_scoped,
                    "class": f"{c.connector_class.__module__}.{c.connector_class.__name__}",
                }
            except KeyError:
                available = ", ".join(ConnectorRegistry.list_available())
                console.print(f"[red]Unknown connector: {connector_name}[/red]")
                console.print(f"[dim]Available: {available}[/dim]")
                sys.exit(1)

        console.print(f"\n[bold cyan]{info['name']}[/bold cyan]")
        console.print(f"  [dim]Description:[/dim] {info.get('description') or 'No description'}")
        console.print(f"  [dim]Category:[/dim] {info.get('category', 'unknown')}")
        console.print(f"  [dim]User-scoped:[/dim] {'Yes' if info.get('user_scoped') else 'No'}")

        requires = info.get("requires", [])
        if requires:
            console.print(f"  [dim]Dependencies:[/dim] {', '.join(requires)}")
        else:
            console.print("  [dim]Dependencies:[/dim] None (core)")

        if "class" in info:
            console.print(f"  [dim]Class:[/dim] {info['class']}")

        # Show connection arguments (local mode only)
        if not backend_config.remote_url:
            from nexus.backends import ConnectorRegistry

            connection_args = ConnectorRegistry.get_connection_args(connector_name)
            if connection_args:
                console.print("\n  [bold]Connection Arguments:[/bold]")

                # Create table for args
                args_table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
                args_table.add_column("Name", style="green")
                args_table.add_column("Type", style="yellow")
                args_table.add_column("Required", style="cyan")
                args_table.add_column("Description")

                for arg_name, arg in connection_args.items():
                    required_str = "[red]Yes[/red]" if arg.required else "No"
                    type_str = arg.type.value
                    if arg.secret:
                        type_str += " [dim](secret)[/dim]"
                    desc = arg.description
                    if arg.default is not None:
                        desc += f" [dim](default: {arg.default})[/dim]"
                    if arg.env_var:
                        desc += f" [dim](env: {arg.env_var})[/dim]"
                    args_table.add_row(arg_name, type_str, required_str, desc)

                console.print(args_table)

        console.print()

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register connector commands to the main CLI group."""
    cli.add_command(connectors_group)
