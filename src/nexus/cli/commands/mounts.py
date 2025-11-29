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


@mounts_group.command(name="sync")
@click.argument("mount_point", type=str, required=False, default=None)
@click.option("--path", type=str, default=None, help="Specific path within mount to sync")
@click.option("--no-cache", is_flag=True, help="Skip content cache sync (metadata only)")
@click.option(
    "--include",
    type=str,
    multiple=True,
    help="Glob patterns to include (e.g., --include '*.py' --include '*.md')",
)
@click.option(
    "--exclude",
    type=str,
    multiple=True,
    help="Glob patterns to exclude (e.g., --exclude '*.pyc' --exclude '.git/*')",
)
@click.option("--embeddings", is_flag=True, help="Generate embeddings for semantic search")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without making changes")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@add_backend_options
def sync_mount(
    mount_point: str | None,
    path: str | None,
    no_cache: bool,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    embeddings: bool,
    dry_run: bool,
    output_json: bool,
    backend_config: BackendConfig,
) -> None:
    """Sync metadata and content from connector backend(s).

    Scans the external storage (e.g., GCS bucket) and updates the Nexus database
    with files that were added externally. Also populates the content cache for
    fast grep/search operations.

    If no MOUNT_POINT is specified, syncs ALL connector mounts.

    Examples:
        # Sync all connector mounts
        nexus mounts sync

        # Sync specific mount
        nexus mounts sync /mnt/gcs

        # Sync specific directory within a mount
        nexus mounts sync /mnt/gcs --path reports/2024

        # Sync single file
        nexus mounts sync /mnt/gcs --path data/report.pdf

        # Sync only Python files
        nexus mounts sync /mnt/gcs --include '*.py' --include '*.md'

        # Sync metadata only (skip content cache)
        nexus mounts sync /mnt/gcs --no-cache

        # Dry run to see what would be synced
        nexus mounts sync /mnt/gcs --dry-run
    """
    try:
        # Get filesystem (works with both local and remote)
        nx = get_filesystem(backend_config)

        # Convert tuples to lists for include/exclude
        include_patterns = list(include) if include else None
        exclude_patterns = list(exclude) if exclude else None

        if not output_json:
            if mount_point:
                console.print(f"[yellow]Syncing mount: {mount_point}...[/yellow]")
            else:
                console.print("[yellow]Syncing all connector mounts...[/yellow]")

            if dry_run:
                console.print("[cyan](dry run - no changes will be made)[/cyan]")

        try:
            result = nx.sync_mount(  # type: ignore[attr-defined]
                mount_point=mount_point,
                path=path,
                recursive=True,
                dry_run=dry_run,
                sync_content=not no_cache,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                generate_embeddings=embeddings,
            )
        except AttributeError:
            console.print("[red]Error:[/red] This Nexus instance doesn't support sync_mount")
            console.print("[yellow]Hint:[/yellow] Make sure you're using the latest Nexus version")
            sys.exit(1)

        if output_json:
            # Output as JSON
            import json as json_lib

            console.print(json_lib.dumps(result, indent=2))
        else:
            # Pretty output
            console.print()
            console.print("[bold cyan]Sync Results:[/bold cyan]")

            # Show mount-level stats if syncing all
            if mount_point is None and "mounts_synced" in result:
                console.print(f"  Mounts synced: [green]{result['mounts_synced']}[/green]")
                console.print(f"  Mounts skipped: [yellow]{result['mounts_skipped']}[/yellow]")
                console.print()

            console.print("[bold]Metadata:[/bold]")
            console.print(f"  Files scanned: [cyan]{result.get('files_scanned', 0)}[/cyan]")
            console.print(f"  Files created: [green]{result.get('files_created', 0)}[/green]")
            console.print(f"  Files updated: [cyan]{result.get('files_updated', 0)}[/cyan]")
            console.print(f"  Files deleted: [red]{result.get('files_deleted', 0)}[/red]")

            if not no_cache:
                console.print()
                console.print("[bold]Cache:[/bold]")
                console.print(f"  Files cached: [green]{result.get('cache_synced', 0)}[/green]")
                cache_skipped = result.get("cache_skipped", 0)
                if cache_skipped > 0:
                    console.print(f"  Files skipped: [dim]{cache_skipped}[/dim] (already cached)")
                cache_bytes = result.get("cache_bytes", 0)
                if cache_bytes > 1024 * 1024:
                    console.print(
                        f"  Bytes cached: [cyan]{cache_bytes / 1024 / 1024:.2f} MB[/cyan]"
                    )
                elif cache_bytes > 1024:
                    console.print(f"  Bytes cached: [cyan]{cache_bytes / 1024:.2f} KB[/cyan]")
                else:
                    console.print(f"  Bytes cached: [cyan]{cache_bytes} bytes[/cyan]")

            if embeddings:
                console.print()
                console.print("[bold]Embeddings:[/bold]")
                console.print(
                    f"  Generated: [green]{result.get('embeddings_generated', 0)}[/green]"
                )

            # Show errors if any
            errors = result.get("errors", [])
            if errors:
                console.print()
                console.print(f"[bold red]Errors ({len(errors)}):[/bold red]")
                for error in errors[:5]:
                    console.print(f"  [red]•[/red] {error}")
                if len(errors) > 5:
                    console.print(f"  [red]... and {len(errors) - 5} more[/red]")

            console.print()
            if dry_run:
                console.print("[cyan]Dry run complete - no changes made[/cyan]")
            else:
                console.print("[green]✓[/green] Sync complete")

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register mount commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mounts_group)
