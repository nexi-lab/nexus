"""Nexus CLI Connector Commands.

Commands for discovering and inspecting available connectors:
- nexus connectors list - List all registered connectors
- nexus connectors info - Show connector details

Connects to a remote Nexus instance via RPC.
"""

import sys
from typing import Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
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

    Examples:
        # List all connectors
        nexus connectors list --remote-url http://localhost:2026

        # List only storage connectors
        nexus connectors list --category storage

        # Show details for a specific connector
        nexus connectors info gcs_connector
    """
    pass


def _list_connectors_remote(nx: Any, category: str | None) -> list[dict[str, Any]]:
    """List connectors from remote server via RPC."""
    result: list[dict[str, Any]] = nx.service("mount").list_connectors_sync(category=category)
    return result


@connectors_group.command(name="list")
@click.option(
    "--category",
    "-c",
    type=str,
    default=None,
    help="Filter by category (storage, api, oauth, database)",
)
@add_output_options
@add_backend_options
def list_connectors(
    category: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all registered connectors.

    Shows all available connector types that can be used with 'nexus mounts add'.

    Examples:
        # List connectors from remote server
        nexus connectors list --remote-url http://localhost:2026

        # List only storage connectors
        nexus connectors list --category storage

        # Output as JSON
        nexus connectors list --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)
        try:
            with timing.phase("server"):
                connectors = _list_connectors_remote(nx, category)
        except AttributeError:
            console.print("[red]Error:[/red] Server doesn't support list_connectors")
            console.print("[yellow]Hint:[/yellow] Update server to latest Nexus version")
            sys.exit(1)

        if not connectors:
            if category:
                console.print(f"[yellow]No connectors found in category '{category}'[/yellow]")
            else:
                console.print("[yellow]No connectors registered[/yellow]")
            return

        def _render(data: list[dict[str, Any]]) -> None:
            from rich.table import Table

            table = Table(title="Available Connectors", show_header=True, header_style="bold cyan")
            table.add_column("Name", style="green")
            table.add_column("Description")
            table.add_column("Category", style="yellow")
            table.add_column("Dependencies", style="dim")

            for c in data:
                deps = ", ".join(c["requires"]) if c.get("requires") else "-"
                table.add_row(
                    c["name"],
                    c.get("description", ""),
                    c.get("category", ""),
                    deps,
                )

            console.print(table)
            console.print(f"\n[dim]Total: {len(data)} connectors[/dim]")

        render_output(
            data=connectors,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        handle_error(e)


@connectors_group.command(name="info")
@click.argument("connector_name", type=str)
@add_backend_options
def connector_info(
    connector_name: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show details for a specific connector.

    CONNECTOR_NAME: The connector identifier (e.g., gcs_connector, s3_connector)

    Examples:
        nexus connectors info gcs_connector --remote-url http://localhost:2026
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)
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

        console.print()

    except Exception as e:
        handle_error(e)


@connectors_group.command(name="capabilities")
@click.argument("name", required=False, default=None)
@add_output_options
@add_backend_options
def connectors_capabilities(
    name: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show connector capabilities.

    Without arguments, shows capabilities for all connectors.
    With a connector name, shows detailed capabilities for that connector.

    Examples:
        nexus connectors capabilities
        nexus connectors capabilities gcs_connector
    """
    timing = CommandTiming()
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)
        try:
            with timing.phase("server"):
                all_connectors = _list_connectors_remote(nx, None)
        except AttributeError:
            console.print("[red]Error:[/red] Server doesn't support list_connectors")
            console.print("[yellow]Hint:[/yellow] Update server to latest Nexus version")
            sys.exit(1)

        if name:
            match = [
                c for c in all_connectors if c.get("name") == name or c.get("connector_id") == name
            ]
            if not match:
                available = ", ".join(c["name"] for c in all_connectors)
                console.print(f"[red]Error:[/red] Connector '{name}' not found")
                console.print(f"[dim]Available: {available}[/dim]")
                sys.exit(1)
            connectors_to_show = match
        else:
            connectors_to_show = all_connectors

        def _render(data: list[dict[str, Any]]) -> None:
            from rich.table import Table

            table = Table(
                title="Connector Capabilities",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Name", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Capabilities", style="yellow")

            for c in data:
                caps = c.get("capabilities", [])
                if isinstance(caps, list):
                    caps_str = ", ".join(str(cap) for cap in caps) if caps else "none"
                else:
                    caps_str = str(caps)
                table.add_row(
                    c.get("name", "?"),
                    c.get("category", c.get("type", "?")),
                    caps_str,
                )

            console.print(table)
            console.print(f"\n[dim]Total: {len(data)} connectors[/dim]")

        render_output(
            data=connectors_to_show,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except SystemExit:
        raise
    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register connector commands to the main CLI group."""
    cli.add_command(connectors_group)
