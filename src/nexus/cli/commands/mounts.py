"""Nexus CLI Mount Management Commands.

Commands for managing persistent mount configurations:
- nexus mounts add - Add a new backend mount
- nexus mounts remove - Remove a mount
- nexus mounts list - List all mounts
- nexus mounts info - Show mount details

Note: All commands work with both local and remote Nexus instances.
For remote servers, commands call the RPC API (add_mount, remove_mount, etc.).
For local instances, commands interact directly with the NexusFS methods.
"""

from __future__ import annotations

import json
import sys

import click

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


@click.group(name="mounts")
def mounts_group() -> None:
    """Manage backend mounts.

    Persistent mount management allows you to add/remove backend mounts
    dynamically. Mounts are stored in the database and restored on restart.

    Use Cases:
    - Mount user's personal Google Drive when they join org
    - Mount team shared buckets
    - Mount legacy storage for migration

    Examples:
        # List all mounts
        nexus mounts list

        # Add a new mount
        nexus mounts add /personal/alice google_drive '{"access_token":"..."}' --priority 10

        # Remove a mount
        nexus mounts remove /personal/alice

        # Show mount details
        nexus mounts info /personal/alice
    """
    pass


@mounts_group.command(name="add")
@click.argument("mount_point", type=str)
@click.argument("backend_type", type=str)
@click.argument("config_json", type=str)
@click.option("--priority", type=int, default=0, help="Mount priority (higher = preferred)")
@click.option("--readonly", is_flag=True, help="Mount as read-only")
@click.option("--owner", type=str, default=None, help="Owner user ID")
@click.option("--tenant", type=str, default=None, help="Tenant ID")
@add_backend_options
def add_mount(
    mount_point: str,
    backend_type: str,
    config_json: str,
    priority: int,
    readonly: bool,
    owner: str | None,
    tenant: str | None,
    backend_config: BackendConfig,
) -> None:
    """Add a new backend mount.

    Saves mount configuration to database and mounts the backend immediately.

    MOUNT_POINT: Virtual path where backend will be mounted (e.g., /personal/alice)

    BACKEND_TYPE: Type of backend (e.g., google_drive, gcs, local, s3)

    BACKEND_CONFIG: Backend configuration as JSON string

    Examples:
        # Mount local directory
        nexus mounts add /external/data local '{"root_path":"/path/to/data"}'

        # Mount Google Cloud Storage
        nexus mounts add /cloud/bucket gcs '{"bucket_name":"my-bucket"}' --priority 10

        # Mount with ownership
        nexus mounts add /personal/alice google_drive '{"access_token":"..."}' \\
            --owner "google:alice123" --tenant "acme"
    """
    try:
        # Parse backend config JSON
        try:
            config_dict = json.loads(config_json)
        except json.JSONDecodeError as e:
            console.print(f"[red]Error:[/red] Invalid JSON in config_json: {e}")
            sys.exit(1)

        # Get filesystem (works with both local and remote)
        nx = get_filesystem(backend_config)

        # Call add_mount - works for both RemoteNexusFS (RPC) and NexusFS (local)
        console.print("[yellow]Adding mount...[/yellow]")

        try:
            mount_id = nx.add_mount(
                mount_point=mount_point,
                backend_type=backend_type,
                backend_config=config_dict,
                priority=priority,
                readonly=readonly,
            )
            console.print(f"[green]✓[/green] Mount added successfully (ID: {mount_id})")
        except AttributeError:
            # Fallback for older NexusFS that doesn't have add_mount
            # This shouldn't happen in normal usage
            console.print("[red]Error:[/red] This Nexus instance doesn't support dynamic mounts")
            console.print("[yellow]Hint:[/yellow] Make sure you're using the latest Nexus version")
            sys.exit(1)

        console.print()
        console.print("[bold cyan]Mount Details:[/bold cyan]")
        console.print(f"  Mount Point: [cyan]{mount_point}[/cyan]")
        console.print(f"  Backend Type: [cyan]{backend_type}[/cyan]")
        console.print(f"  Priority: [cyan]{priority}[/cyan]")
        console.print(f"  Read-Only: [cyan]{readonly}[/cyan]")
        if owner:
            console.print(f"  Owner: [cyan]{owner}[/cyan]")
        if tenant:
            console.print(f"  Tenant: [cyan]{tenant}[/cyan]")

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        handle_error(e)


@mounts_group.command(name="remove")
@click.argument("mount_point", type=str)
@add_backend_options
def remove_mount(mount_point: str, backend_config: BackendConfig) -> None:
    """Remove a backend mount.

    Removes mount configuration from database. The mount will be unmounted
    on next server restart.

    Examples:
        nexus mounts remove /personal/alice
        nexus mounts remove /cloud/bucket
    """
    try:
        # Get filesystem (works with both local and remote)
        nx = get_filesystem(backend_config)

        # Call remove_mount - works for both RemoteNexusFS (RPC) and NexusFS (local)
        console.print(f"[yellow]Removing mount at {mount_point}...[/yellow]")

        try:
            success = nx.remove_mount(mount_point)
            if success:
                console.print("[green]✓[/green] Mount removed successfully")
            else:
                console.print(f"[red]Error:[/red] Mount not found: {mount_point}")
                sys.exit(1)
        except AttributeError:
            console.print("[red]Error:[/red] This Nexus instance doesn't support dynamic mounts")
            console.print("[yellow]Hint:[/yellow] Make sure you're using the latest Nexus version")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="list")
@click.option("--owner", type=str, default=None, help="Filter by owner user ID")
@click.option("--tenant", type=str, default=None, help="Filter by tenant ID")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@add_backend_options
def list_mounts(
    owner: str | None, tenant: str | None, output_json: bool, backend_config: BackendConfig
) -> None:
    """List all persisted mounts.

    Shows all backend mounts stored in the database, with optional filtering
    by owner or tenant.

    Examples:
        # List all mounts
        nexus mounts list

        # List mounts for specific user
        nexus mounts list --owner "google:alice123"

        # List mounts for specific tenant
        nexus mounts list --tenant "acme"

        # Output as JSON
        nexus mounts list --json
    """
    try:
        # Get filesystem (works with both local and remote)
        nx = get_filesystem(backend_config)

        # Call list_mounts - works for both RemoteNexusFS (RPC) and NexusFS (local)
        try:
            mounts = nx.list_mounts()
        except AttributeError:
            console.print("[red]Error:[/red] This Nexus instance doesn't support listing mounts")
            console.print("[yellow]Hint:[/yellow] Make sure you're using the latest Nexus version")
            sys.exit(1)

        # Note: owner/tenant filtering not yet supported in remote mode
        if owner or tenant:
            console.print(
                "[yellow]Warning:[/yellow] Filtering by owner/tenant not yet supported. Showing all mounts."
            )

        if output_json:
            # Output as JSON
            import json as json_lib

            console.print(json_lib.dumps(mounts, indent=2))
        else:
            # Pretty table output
            if not mounts:
                console.print("[yellow]No mounts found[/yellow]")
                return

            console.print(f"\n[bold cyan]Active Mounts ({len(mounts)} total)[/bold cyan]\n")

            for mount in mounts:
                console.print(f"[bold]{mount['mount_point']}[/bold]")
                console.print(
                    f"  Backend Type: [cyan]{mount.get('backend_type', 'unknown')}[/cyan]"
                )
                console.print(f"  Priority: [cyan]{mount['priority']}[/cyan]")
                console.print(f"  Read-Only: [cyan]{'Yes' if mount['readonly'] else 'No'}[/cyan]")
                console.print()

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="info")
@click.argument("mount_point", type=str)
@click.option(
    "--show-config", is_flag=True, help="Show backend configuration (may contain secrets)"
)
@add_backend_options
def mount_info(mount_point: str, show_config: bool, backend_config: BackendConfig) -> None:
    """Show detailed information about a mount.

    Examples:
        nexus mounts info /personal/alice
        nexus mounts info /cloud/bucket --show-config
    """
    try:
        # Get filesystem (works with both local and remote)
        nx = get_filesystem(backend_config)

        # Call get_mount - works for both RemoteNexusFS (RPC) and NexusFS (local)
        try:
            mount = nx.get_mount(mount_point)
        except AttributeError:
            console.print("[red]Error:[/red] This Nexus instance doesn't support mount info")
            console.print("[yellow]Hint:[/yellow] Make sure you're using the latest Nexus version")
            sys.exit(1)

        if not mount:
            console.print(f"[red]Error:[/red] Mount not found: {mount_point}")
            sys.exit(1)

        # Display mount info
        console.print(f"\n[bold cyan]Mount Information: {mount_point}[/bold cyan]\n")

        console.print(f"[bold]Backend Type:[/bold] {mount.get('backend_type', 'unknown')}")
        console.print(f"[bold]Priority:[/bold] {mount['priority']}")
        console.print(f"[bold]Read-Only:[/bold] {'Yes' if mount['readonly'] else 'No'}")

        # Note: show_config not supported yet for active mounts (config not returned by router)
        if show_config:
            console.print(
                "\n[yellow]Note:[/yellow] Backend configuration display not yet supported for active mounts"
            )

        console.print()

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register mount commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mounts_group)
