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
@click.option("--async", "run_async", is_flag=True, help="Run sync in background (returns job ID)")
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
    run_async: bool,
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

        # Run sync in background (async)
        nexus mounts sync /mnt/gmail --async
    """
    try:
        # Get filesystem (works with both local and remote)
        nx = get_filesystem(backend_config)

        # Convert tuples to lists for include/exclude
        include_patterns = list(include) if include else None
        exclude_patterns = list(exclude) if exclude else None

        # Handle async mode (Issue #609)
        if run_async:
            if mount_point is None:
                console.print("[red]Error:[/red] --async requires a mount point")
                console.print("[yellow]Hint:[/yellow] Use: nexus mounts sync /mnt/xxx --async")
                sys.exit(1)

            try:
                result = nx.sync_mount_async(  # type: ignore[attr-defined]
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
                console.print("[red]Error:[/red] This Nexus instance doesn't support async sync")
                console.print("[yellow]Hint:[/yellow] Make sure you're using Nexus >= 0.6.0")
                sys.exit(1)

            if output_json:
                import json as json_lib

                console.print(json_lib.dumps(result, indent=2))
            else:
                console.print(f"[green]Job started:[/green] {result['job_id']}")
                console.print(f"  Mount: {result['mount_point']}")
                console.print(f"  Status: {result['status']}")
                console.print()
                console.print("[dim]Monitor progress with:[/dim]")
                console.print(f"  nexus mounts sync-status {result['job_id']}")
            return

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


@mounts_group.command(name="sync-status")
@click.argument("job_id", type=str, required=False, default=None)
@click.option("--watch", is_flag=True, help="Watch progress until completion")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@add_backend_options
def sync_status(
    job_id: str | None,
    watch: bool,
    output_json: bool,
    backend_config: BackendConfig,
) -> None:
    """Show sync job status and progress.

    If JOB_ID is provided, shows status of that specific job.
    If no JOB_ID, shows recent running jobs.

    Examples:
        # Show status of a specific job
        nexus mounts sync-status abc123

        # Watch progress until completion
        nexus mounts sync-status abc123 --watch

        # List recent running jobs
        nexus mounts sync-status
    """
    import time

    try:
        nx = get_filesystem(backend_config)

        if job_id:
            # Show specific job
            try:
                job = nx.get_sync_job(job_id)  # type: ignore[attr-defined]
            except AttributeError:
                console.print("[red]Error:[/red] This Nexus instance doesn't support sync jobs")
                sys.exit(1)

            if not job:
                console.print(f"[red]Error:[/red] Job not found: {job_id}")
                sys.exit(1)

            if output_json:
                import json as json_lib

                console.print(json_lib.dumps(job, indent=2))
                return

            # Initial display
            _display_job_status(job)

            # Watch mode
            if watch and job["status"] in ("pending", "running"):
                console.print()
                console.print("[dim]Watching progress (Ctrl+C to stop)...[/dim]")
                try:
                    while True:
                        time.sleep(2)
                        job = nx.get_sync_job(job_id)  # type: ignore[attr-defined]
                        if not job:
                            break
                        # Clear and redisplay
                        console.print("\033[2J\033[H", end="")  # Clear screen
                        _display_job_status(job)
                        if job["status"] not in ("pending", "running"):
                            break
                except KeyboardInterrupt:
                    console.print("\n[dim]Stopped watching[/dim]")
        else:
            # List recent running jobs
            try:
                jobs = nx.list_sync_jobs(status="running", limit=10)  # type: ignore[attr-defined]
            except AttributeError:
                console.print("[red]Error:[/red] This Nexus instance doesn't support sync jobs")
                sys.exit(1)

            if output_json:
                import json as json_lib

                console.print(json_lib.dumps(jobs, indent=2))
                return

            if not jobs:
                console.print("[yellow]No running sync jobs[/yellow]")
                console.print("[dim]Use 'nexus mounts sync-jobs' to see all jobs[/dim]")
                return

            console.print(f"[bold cyan]Running Sync Jobs ({len(jobs)})[/bold cyan]")
            console.print()
            for job in jobs:
                console.print(f"  [bold]{job['id'][:8]}...[/bold]")
                console.print(f"    Mount: {job['mount_point']}")
                console.print(f"    Progress: {job['progress_pct']}%")
                console.print()

    except Exception as e:
        handle_error(e)


def _display_job_status(job: dict) -> None:
    """Display job status in a formatted way."""
    status_colors = {
        "pending": "yellow",
        "running": "cyan",
        "completed": "green",
        "failed": "red",
        "cancelled": "yellow",
    }
    status = job["status"]
    color = status_colors.get(status, "white")

    console.print(f"[bold cyan]Sync Job: {job['id']}[/bold cyan]")
    console.print()
    console.print(f"  Mount: [bold]{job['mount_point']}[/bold]")
    console.print(f"  Status: [{color}]{status}[/{color}]")
    console.print(f"  Progress: {job['progress_pct']}%")

    if job.get("progress_detail"):
        detail = job["progress_detail"]
        if detail.get("files_scanned"):
            console.print(f"  Files scanned: {detail['files_scanned']}")
        if detail.get("current_path"):
            path = detail["current_path"]
            if len(path) > 50:
                path = "..." + path[-47:]
            console.print(f"  Current: {path}")

    if job.get("created_at"):
        console.print(f"  Created: {job['created_at']}")
    if job.get("started_at"):
        console.print(f"  Started: {job['started_at']}")
    if job.get("completed_at"):
        console.print(f"  Completed: {job['completed_at']}")

    if job.get("error_message"):
        console.print()
        console.print(f"  [red]Error: {job['error_message']}[/red]")

    if job.get("result"):
        console.print()
        console.print("  [bold]Results:[/bold]")
        result = job["result"]
        console.print(f"    Files scanned: {result.get('files_scanned', 0)}")
        console.print(f"    Files created: {result.get('files_created', 0)}")
        console.print(f"    Files updated: {result.get('files_updated', 0)}")
        console.print(f"    Cache synced: {result.get('cache_synced', 0)}")


@mounts_group.command(name="sync-cancel")
@click.argument("job_id", type=str)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@add_backend_options
def sync_cancel(
    job_id: str,
    output_json: bool,
    backend_config: BackendConfig,
) -> None:
    """Cancel a running sync job.

    Examples:
        nexus mounts sync-cancel abc123
    """
    try:
        nx = get_filesystem(backend_config)

        try:
            result = nx.cancel_sync_job(job_id)  # type: ignore[attr-defined]
        except AttributeError:
            console.print("[red]Error:[/red] This Nexus instance doesn't support sync jobs")
            sys.exit(1)

        if output_json:
            import json as json_lib

            console.print(json_lib.dumps(result, indent=2))
        else:
            if result["success"]:
                console.print(f"[green]Cancellation requested for job {job_id}[/green]")
                console.print("[dim]Job will stop at next checkpoint[/dim]")
            else:
                console.print(f"[red]Failed:[/red] {result['message']}")
                sys.exit(1)

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="sync-jobs")
@click.option("--mount", type=str, default=None, help="Filter by mount point")
@click.option(
    "--status",
    type=click.Choice(["pending", "running", "completed", "failed", "cancelled"]),
    default=None,
    help="Filter by status",
)
@click.option("--limit", type=int, default=20, help="Maximum jobs to show")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@add_backend_options
def sync_jobs(
    mount: str | None,
    status: str | None,
    limit: int,
    output_json: bool,
    backend_config: BackendConfig,
) -> None:
    """List sync jobs.

    Examples:
        # List all recent jobs
        nexus mounts sync-jobs

        # List jobs for a specific mount
        nexus mounts sync-jobs --mount /mnt/gmail

        # List only failed jobs
        nexus mounts sync-jobs --status failed
    """
    try:
        nx = get_filesystem(backend_config)

        try:
            jobs = nx.list_sync_jobs(mount_point=mount, status=status, limit=limit)  # type: ignore[attr-defined]
        except AttributeError:
            console.print("[red]Error:[/red] This Nexus instance doesn't support sync jobs")
            sys.exit(1)

        if output_json:
            import json as json_lib

            console.print(json_lib.dumps(jobs, indent=2))
            return

        if not jobs:
            console.print("[yellow]No sync jobs found[/yellow]")
            return

        status_colors = {
            "pending": "yellow",
            "running": "cyan",
            "completed": "green",
            "failed": "red",
            "cancelled": "yellow",
        }

        console.print(f"[bold cyan]Sync Jobs ({len(jobs)} shown)[/bold cyan]")
        console.print()

        for job in jobs:
            job_status = job["status"]
            color = status_colors.get(job_status, "white")
            job_id_short = job["id"][:8]

            console.print(
                f"  [bold]{job_id_short}...[/bold]  "
                f"[{color}]{job_status:10}[/{color}]  "
                f"{job['progress_pct']:3}%  "
                f"{job['mount_point']}"
            )

        console.print()
        console.print("[dim]Use 'nexus mounts sync-status <job_id>' for details[/dim]")

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register mount commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mounts_group)
