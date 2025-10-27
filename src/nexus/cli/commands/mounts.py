"""Nexus CLI Mount Management Commands.

Commands for managing persistent mount configurations:
- nexus mounts add - Add a new backend mount
- nexus mounts remove - Remove a mount
- nexus mounts list - List all mounts
- nexus mounts info - Show mount details
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
@click.option("--description", type=str, default=None, help="Mount description")
@add_backend_options
def add_mount(
    mount_point: str,
    backend_type: str,
    config_json: str,
    priority: int,
    readonly: bool,
    owner: str | None,
    tenant: str | None,
    description: str | None,
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
            --owner "google:alice123" --tenant "acme" --description "Alice's Google Drive"
    """
    try:
        from nexus.core.mount_manager import MountManager

        # Parse backend config JSON
        try:
            config_dict = json.loads(config_json)
        except json.JSONDecodeError as e:
            console.print(f"[red]Error:[/red] Invalid JSON in config_json: {e}")
            sys.exit(1)

        # Get filesystem
        nx = get_filesystem(backend_config)

        # Get mount manager
        if not hasattr(nx, "metadata") or not hasattr(nx.metadata, "SessionLocal"):
            console.print(
                "[red]Error:[/red] Mount persistence requires a NexusFS instance with metadata store"
            )
            sys.exit(1)

        manager = MountManager(nx.metadata.SessionLocal)

        # Save mount to database
        console.print("[yellow]Saving mount configuration...[/yellow]")
        mount_id = manager.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=config_dict,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner,
            tenant_id=tenant,
            description=description,
        )

        console.print(f"[green]✓[/green] Mount configuration saved (ID: {mount_id})")

        # Try to mount immediately
        # Note: This requires a backend factory - for now we just save to DB
        # The mount will be restored on next server restart
        console.print(
            "[yellow]Note:[/yellow] Mount saved to database. Restart server or use programmatic API to activate."
        )

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
        from nexus.core.mount_manager import MountManager

        # Get filesystem
        nx = get_filesystem(backend_config)

        # Get mount manager
        if not hasattr(nx, "metadata") or not hasattr(nx.metadata, "SessionLocal"):
            console.print(
                "[red]Error:[/red] Mount persistence requires a NexusFS instance with metadata store"
            )
            sys.exit(1)

        manager = MountManager(nx.metadata.SessionLocal)

        # Remove from database
        console.print(f"[yellow]Removing mount at {mount_point}...[/yellow]")
        if manager.remove_mount(mount_point):
            console.print("[green]✓[/green] Mount removed from database")
            console.print(
                "[yellow]Note:[/yellow] Restart server or use programmatic API to deactivate."
            )
        else:
            console.print(f"[red]Error:[/red] Mount not found: {mount_point}")
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
        from nexus.core.mount_manager import MountManager

        # Get filesystem
        nx = get_filesystem(backend_config)

        # Get mount manager
        if not hasattr(nx, "metadata") or not hasattr(nx.metadata, "SessionLocal"):
            console.print(
                "[red]Error:[/red] Mount persistence requires a NexusFS instance with metadata store"
            )
            sys.exit(1)

        manager = MountManager(nx.metadata.SessionLocal)

        # Get mounts
        mounts = manager.list_mounts(owner_user_id=owner, tenant_id=tenant)

        if output_json:
            # Output as JSON
            import json as json_lib

            # Convert datetime to string for JSON serialization
            for m in mounts:
                m["created_at"] = m["created_at"].isoformat() if m["created_at"] else None
                m["updated_at"] = m["updated_at"].isoformat() if m["updated_at"] else None

            console.print(json_lib.dumps(mounts, indent=2))
        else:
            # Pretty table output
            if not mounts:
                console.print("[yellow]No mounts found[/yellow]")
                return

            console.print(f"\n[bold cyan]Persisted Mounts ({len(mounts)} total)[/bold cyan]\n")

            for mount in mounts:
                console.print(f"[bold]{mount['mount_point']}[/bold]")
                console.print(f"  Backend Type: [cyan]{mount['backend_type']}[/cyan]")
                console.print(f"  Priority: [cyan]{mount['priority']}[/cyan]")
                console.print(f"  Read-Only: [cyan]{'Yes' if mount['readonly'] else 'No'}[/cyan]")

                if mount["owner_user_id"]:
                    console.print(f"  Owner: [cyan]{mount['owner_user_id']}[/cyan]")

                if mount["tenant_id"]:
                    console.print(f"  Tenant: [cyan]{mount['tenant_id']}[/cyan]")

                if mount["description"]:
                    console.print(f"  Description: {mount['description']}")

                console.print(f"  Created: {mount['created_at'].strftime('%Y-%m-%d %H:%M:%S')}")
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
        from nexus.core.mount_manager import MountManager

        # Get filesystem
        nx = get_filesystem(backend_config)

        # Get mount manager
        if not hasattr(nx, "metadata") or not hasattr(nx.metadata, "SessionLocal"):
            console.print(
                "[red]Error:[/red] Mount persistence requires a NexusFS instance with metadata store"
            )
            sys.exit(1)

        manager = MountManager(nx.metadata.SessionLocal)

        # Get mount
        mount = manager.get_mount(mount_point)

        if not mount:
            console.print(f"[red]Error:[/red] Mount not found: {mount_point}")
            sys.exit(1)

        # Display mount info
        console.print(f"\n[bold cyan]Mount Information: {mount_point}[/bold cyan]\n")

        console.print(f"[bold]Mount ID:[/bold] {mount['mount_id']}")
        console.print(f"[bold]Backend Type:[/bold] {mount['backend_type']}")
        console.print(f"[bold]Priority:[/bold] {mount['priority']}")
        console.print(f"[bold]Read-Only:[/bold] {'Yes' if mount['readonly'] else 'No'}")

        if mount["owner_user_id"]:
            console.print(f"[bold]Owner:[/bold] {mount['owner_user_id']}")

        if mount["tenant_id"]:
            console.print(f"[bold]Tenant:[/bold] {mount['tenant_id']}")

        if mount["description"]:
            console.print(f"[bold]Description:[/bold] {mount['description']}")

        console.print(
            f"[bold]Created:[/bold] {mount['created_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        console.print(
            f"[bold]Updated:[/bold] {mount['updated_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        if show_config:
            console.print("\n[bold]Backend Configuration:[/bold]")
            import json as json_lib

            console.print(json_lib.dumps(mount["backend_config"], indent=2))
        else:
            console.print(
                "\n[dim]Use --show-config to display backend configuration (may contain secrets)[/dim]"
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
