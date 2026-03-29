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

import json
import sys
from typing import Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.theme import console
from nexus.cli.utils import (
    add_backend_options,
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
        nexus mounts add /personal/alice google_drive '{"access_token":"..."}'

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
@click.option("--readonly", is_flag=True, help="Mount as read-only")
@click.option(
    "--io-profile",
    type=click.Choice(["fast_read", "fast_write", "edit", "append_only", "balanced", "archive"]),
    default="balanced",
    help="I/O profile for the mount (default: balanced)",
)
@click.option("--owner", type=str, default=None, help="Owner user ID")
@click.option("--zone", type=str, default=None, help="Zone ID")
@add_backend_options
def add_mount(
    mount_point: str,
    backend_type: str,
    config_json: str,
    readonly: bool,
    io_profile: str,
    owner: str | None,
    zone: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
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
        nexus mounts add /cloud/bucket gcs '{"bucket_name":"my-bucket"}'

        # Mount with ownership
        nexus mounts add /personal/alice google_drive '{"access_token":"..."}' \\
            --owner "google:alice123" --zone "acme"
    """
    import asyncio

    asyncio.run(
        _async_add_mount(
            mount_point,
            backend_type,
            config_json,
            readonly,
            io_profile,
            owner,
            zone,
            remote_url,
            remote_api_key,
        )
    )


async def _async_add_mount(
    mount_point: str,
    backend_type: str,
    config_json: str,
    readonly: bool,
    io_profile: str,
    owner: str | None,
    zone: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        # Parse backend config JSON
        try:
            config_dict = json.loads(config_json)
        except json.JSONDecodeError as e:
            console.print(f"[nexus.error]Error:[/nexus.error] Invalid JSON in config_json: {e}")
            sys.exit(1)

        import os

        import httpx

        base_url = remote_url or os.environ.get("NEXUS_URL", "")
        api_key = remote_api_key or os.environ.get("NEXUS_API_KEY", "")
        if not base_url:
            console.print("[nexus.error]Error:[/nexus.error] NEXUS_URL required")
            console.print("[nexus.warning]Hint:[/nexus.warning] eval $(nexus env)")
            sys.exit(1)

        console.print("[nexus.warning]Adding mount...[/nexus.warning]")

        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                f"{base_url.rstrip('/')}/api/v2/connectors/mount",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "connector_type": backend_type,
                    "mount_point": mount_point,
                    "config": config_dict,
                    "readonly": readonly,
                },
            )
            resp.raise_for_status()
            result = resp.json()

        if result.get("mounted"):
            mount_id = result.get("mount_point", mount_point)
            console.print(f"[nexus.success]\u2713[/nexus.success] Mount added successfully (ID: {mount_id})")
        else:
            console.print(f"[nexus.error]Error:[/nexus.error] {result.get('error', 'Mount failed')}")
            sys.exit(1)

        console.print()
        console.print("[bold nexus.value]Mount Details:[/bold nexus.value]")
        console.print(f"  Mount Point: [nexus.path]{mount_point}[/nexus.path]")
        console.print(f"  Backend Type: [nexus.value]{backend_type}[/nexus.value]")
        console.print(f"  Read-Only: [nexus.value]{readonly}[/nexus.value]")
        console.print(f"  IO Profile: [nexus.value]{io_profile}[/nexus.value]")
        if owner:
            console.print(f"  Owner: [nexus.value]{owner}[/nexus.value]")
        if zone:
            console.print(f"  Zone: [nexus.value]{zone}[/nexus.value]")

    except ValueError as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        sys.exit(1)
    except Exception as e:
        handle_error(e)


@mounts_group.command(name="remove")
@click.argument("mount_point", type=str)
@add_backend_options
def remove_mount(mount_point: str, remote_url: str | None, remote_api_key: str | None) -> None:
    """Remove a backend mount.

    Removes mount configuration from database. The mount will be unmounted
    on next server restart.

    Examples:
        nexus mounts remove /personal/alice
        nexus mounts remove /cloud/bucket
    """
    import asyncio

    asyncio.run(_async_remove_mount(mount_point, remote_url, remote_api_key))


async def _async_remove_mount(
    mount_point: str, remote_url: str | None, remote_api_key: str | None
) -> None:
    try:
        import os

        import httpx

        base_url = remote_url or os.environ.get("NEXUS_URL", "")
        api_key = remote_api_key or os.environ.get("NEXUS_API_KEY", "")
        if not base_url:
            console.print("[nexus.error]Error:[/nexus.error] NEXUS_URL required")
            console.print("[nexus.warning]Hint:[/nexus.warning] eval $(nexus env)")
            sys.exit(1)

        console.print(f"[nexus.warning]Removing mount at {mount_point}...[/nexus.warning]")

        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                f"{base_url.rstrip('/')}/api/v2/connectors/unmount",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"connector_type": "", "mount_point": mount_point},
            )
            resp.raise_for_status()
            console.print("[nexus.success]\u2713[/nexus.success] Mount removed successfully")

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="list")
@click.option("--owner", type=str, default=None, help="Filter by owner user ID")
@click.option("--zone", type=str, default=None, help="Filter by zone ID")
@add_output_options
@add_backend_options
def list_mounts(
    owner: str | None,
    zone: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all persisted mounts.

    Shows all backend mounts stored in the database, with optional filtering
    by owner or zone.

    Examples:
        # List all mounts
        nexus mounts list

        # List mounts for specific user
        nexus mounts list --owner "google:alice123"

        # List mounts for specific zone
        nexus mounts list --zone "acme"

        # Output as JSON
        nexus mounts list --json
    """
    import asyncio

    asyncio.run(_async_list_mounts(owner, zone, output_opts, remote_url, remote_api_key))


async def _async_list_mounts(
    owner: str | None,
    zone: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    timing = CommandTiming()
    try:
        # List mounts via HTTP API (gRPC port may differ from default)
        with timing.phase("server"):
            import os

            import httpx

            base_url = remote_url or os.environ.get("NEXUS_URL", "")
            api_key = remote_api_key or os.environ.get("NEXUS_API_KEY", "")
            if not base_url:
                console.print("[nexus.error]Error:[/nexus.error] NEXUS_URL required")
                console.print("[nexus.warning]Hint:[/nexus.warning] eval $(nexus env)")
                sys.exit(1)
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(
                    f"{base_url.rstrip('/')}/api/v2/connectors/mounts",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                mounts = resp.json()

        # Note: owner/zone filtering not yet supported in remote mode
        if owner or zone:
            console.print(
                "[nexus.warning]Warning:[/nexus.warning] Filtering by owner/zone not yet supported. Showing all mounts."
            )

        if not mounts:
            console.print("[nexus.warning]No mounts found[/nexus.warning]")
            return

        def _render(data: list[dict[str, Any]]) -> None:
            console.print(f"\n[bold nexus.value]Mounts ({len(data)} total)[/bold nexus.value]\n")

            for mount in data:
                status = mount.get("status", "active")
                if status == "stale":
                    console.print(
                        f"[nexus.warning]{mount['mount_point']}[/nexus.warning]  [nexus.warning](stale)[/nexus.warning]"
                    )
                else:
                    console.print(f"[bold]{mount['mount_point']}[/bold]")
                console.print(
                    f"  Read-Only: [nexus.value]{'Yes' if mount['readonly'] else 'No'}[/nexus.value]"
                )
                console.print(
                    f"  Admin-Only: [nexus.value]{'Yes' if mount.get('admin_only') else 'No'}[/nexus.value]"
                )
                console.print()

        render_output(
            data=mounts,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="info")
@click.argument("mount_point", type=str)
@click.option(
    "--show-config", is_flag=True, help="Show backend configuration (may contain secrets)"
)
@add_backend_options
def mount_info(
    mount_point: str, show_config: bool, remote_url: str | None, remote_api_key: str | None
) -> None:
    """Show detailed information about a mount.

    Examples:
        nexus mounts info /personal/alice
        nexus mounts info /cloud/bucket --show-config
    """
    import asyncio

    asyncio.run(_async_mount_info(mount_point, show_config, remote_url, remote_api_key))


async def _async_mount_info(
    mount_point: str, show_config: bool, remote_url: str | None, remote_api_key: str | None
) -> None:
    try:
        # Get filesystem (works with both local and remote)
        nx = await get_filesystem(remote_url, remote_api_key)

        # Call get_mount via mount_service
        try:
            mount_svc = nx.service("mount")
            assert mount_svc is not None
            mount = await mount_svc.get_mount(mount_point=mount_point)
        except AttributeError:
            console.print(
                "[nexus.error]Error:[/nexus.error] This Nexus instance doesn't support mount info"
            )
            console.print(
                "[nexus.warning]Hint:[/nexus.warning] Make sure you're using the latest Nexus version"
            )
            sys.exit(1)

        if not mount:
            console.print(f"[nexus.error]Error:[/nexus.error] Mount not found: {mount_point}")
            sys.exit(1)

        # Display mount info
        console.print(f"\n[bold nexus.value]Mount Information: {mount_point}[/bold nexus.value]\n")

        console.print(f"[bold]Read-Only:[/bold] {'Yes' if mount['readonly'] else 'No'}")
        console.print(f"[bold]Admin-Only:[/bold] {'Yes' if mount.get('admin_only') else 'No'}")

        # Note: show_config not supported yet for active mounts (config not returned by router)
        if show_config:
            console.print(
                "\n[nexus.warning]Note:[/nexus.warning] Backend configuration display not yet supported for active mounts"
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
@add_output_options
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
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
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
    import asyncio

    asyncio.run(
        _async_sync_mount(
            mount_point,
            path,
            no_cache,
            include,
            exclude,
            embeddings,
            dry_run,
            run_async,
            output_opts,
            remote_url,
            remote_api_key,
        )
    )


async def _async_sync_mount(
    mount_point: str | None,
    path: str | None,
    no_cache: bool,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    embeddings: bool,
    dry_run: bool,
    run_async: bool,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    timing = CommandTiming()
    try:
        # Get filesystem (works with both local and remote)
        nx: Any = await get_filesystem(remote_url, remote_api_key)

        # Convert tuples to lists for include/exclude
        include_patterns = list(include) if include else None
        exclude_patterns = list(exclude) if exclude else None

        # Handle async mode (Issue #609)
        if run_async:
            if mount_point is None:
                console.print("[nexus.error]Error:[/nexus.error] --async requires a mount point")
                console.print(
                    "[nexus.warning]Hint:[/nexus.warning] Use: nexus mounts sync /mnt/xxx --async"
                )
                sys.exit(1)

            with timing.phase("server"):
                try:
                    result = await nx.service("sync_job").sync_mount_async(
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
                    console.print(
                        "[nexus.error]Error:[/nexus.error] This Nexus instance doesn't support async sync"
                    )
                    console.print(
                        "[nexus.warning]Hint:[/nexus.warning] Make sure you're using Nexus >= 0.6.0"
                    )
                    sys.exit(1)

            def _render_async(data: dict[str, Any]) -> None:
                console.print(f"[nexus.success]Job started:[/nexus.success] {data['job_id']}")
                console.print(f"  Mount: {data['mount_point']}")
                console.print(f"  Status: {data['status']}")
                console.print()
                console.print("[nexus.muted]Monitor progress with:[/nexus.muted]")
                console.print(f"  nexus mounts sync-status {data['job_id']}")

            render_output(
                data=result,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_render_async,
            )
            return

        if not output_opts.json_output:
            if mount_point:
                console.print(f"[nexus.warning]Syncing mount: {mount_point}...[/nexus.warning]")
            else:
                console.print("[nexus.warning]Syncing all connector mounts...[/nexus.warning]")

            if dry_run:
                console.print("[nexus.value](dry run - no changes will be made)[/nexus.value]")

        with timing.phase("server"):
            import os

            import httpx

            base_url = remote_url or os.environ.get("NEXUS_URL", "")
            api_key = remote_api_key or os.environ.get("NEXUS_API_KEY", "")
            if not base_url:
                console.print("[nexus.error]Error:[/nexus.error] NEXUS_URL required")
                console.print("[nexus.warning]Hint:[/nexus.warning] eval $(nexus env)")
                sys.exit(1)
            async with httpx.AsyncClient(timeout=120) as http:
                resp = await http.post(
                    f"{base_url.rstrip('/')}/api/v2/connectors/sync",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"mount_point": mount_point, "full_sync": False},
                )
                resp.raise_for_status()
                result = resp.json()

        def _render_sync(data: dict[str, Any]) -> None:
            console.print()
            console.print("[bold nexus.value]Sync Results:[/bold nexus.value]")

            # Show mount-level stats if syncing all
            if mount_point is None and "mounts_synced" in data:
                console.print(
                    f"  Mounts synced: [nexus.success]{data['mounts_synced']}[/nexus.success]"
                )
                console.print(
                    f"  Mounts skipped: [nexus.warning]{data['mounts_skipped']}[/nexus.warning]"
                )
                console.print()

            console.print("[bold]Metadata:[/bold]")
            console.print(
                f"  Files scanned: [nexus.value]{data.get('files_scanned', 0)}[/nexus.value]"
            )
            console.print(
                f"  Files created: [nexus.success]{data.get('files_created', 0)}[/nexus.success]"
            )
            console.print(
                f"  Files updated: [nexus.value]{data.get('files_updated', 0)}[/nexus.value]"
            )
            console.print(
                f"  Files deleted: [nexus.error]{data.get('files_deleted', 0)}[/nexus.error]"
            )

            if not no_cache:
                console.print()
                console.print("[bold]Cache:[/bold]")
                console.print(
                    f"  Files cached: [nexus.success]{data.get('cache_synced', 0)}[/nexus.success]"
                )
                cache_skipped = data.get("cache_skipped", 0)
                if cache_skipped > 0:
                    console.print(
                        f"  Files skipped: [nexus.muted]{cache_skipped}[/nexus.muted] (already cached)"
                    )
                cache_bytes = data.get("cache_bytes", 0)
                if cache_bytes > 1024 * 1024:
                    console.print(
                        f"  Bytes cached: [nexus.value]{cache_bytes / 1024 / 1024:.2f} MB[/nexus.value]"
                    )
                elif cache_bytes > 1024:
                    console.print(
                        f"  Bytes cached: [nexus.value]{cache_bytes / 1024:.2f} KB[/nexus.value]"
                    )
                else:
                    console.print(f"  Bytes cached: [nexus.value]{cache_bytes} bytes[/nexus.value]")

            if embeddings:
                console.print()
                console.print("[bold]Embeddings:[/bold]")
                console.print(
                    f"  Generated: [nexus.success]{data.get('embeddings_generated', 0)}[/nexus.success]"
                )

            # Show errors if any
            errors = data.get("errors", [])
            if errors:
                console.print()
                console.print(f"[bold nexus.error]Errors ({len(errors)}):[/bold nexus.error]")
                for error in errors[:5]:
                    console.print(f"  [nexus.error]\u2022[/nexus.error] {error}")
                if len(errors) > 5:
                    console.print(f"  [nexus.error]... and {len(errors) - 5} more[/nexus.error]")

            console.print()
            if dry_run:
                console.print("[nexus.value]Dry run complete - no changes made[/nexus.value]")
            else:
                console.print("[nexus.success]\u2713[/nexus.success] Sync complete")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render_sync,
        )

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="sync-status")
@click.argument("job_id", type=str, required=False, default=None)
@click.option("--watch", is_flag=True, help="Watch progress until completion")
@add_output_options
@add_backend_options
def sync_status(
    job_id: str | None,
    watch: bool,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
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
    import asyncio

    asyncio.run(_async_sync_status(job_id, watch, output_opts, remote_url, remote_api_key))


async def _async_sync_status(
    job_id: str | None,
    watch: bool,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    import asyncio

    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)

        if job_id:
            # Show specific job
            with timing.phase("server"):
                try:
                    job = await nx.service("sync_job").get_job(job_id)
                except AttributeError:
                    console.print(
                        "[nexus.error]Error:[/nexus.error] This Nexus instance doesn't support sync jobs"
                    )
                    sys.exit(1)

            if not job:
                console.print(f"[nexus.error]Error:[/nexus.error] Job not found: {job_id}")
                sys.exit(1)

            if not watch or job["status"] not in ("pending", "running"):

                def _render_job(data: dict[str, Any]) -> None:
                    _display_job_status(data)

                render_output(
                    data=job,
                    output_opts=output_opts,
                    timing=timing,
                    human_formatter=_render_job,
                )
            else:
                # Watch mode - always human output
                _display_job_status(job)
                console.print()
                console.print("[nexus.muted]Watching progress (Ctrl+C to stop)...[/nexus.muted]")
                try:
                    while True:
                        await asyncio.sleep(2)
                        job = await nx.service("sync_job").get_job(job_id)
                        if not job:
                            break
                        # Clear and redisplay
                        console.print("\033[2J\033[H", end="")  # Clear screen
                        _display_job_status(job)
                        if job["status"] not in ("pending", "running"):
                            break
                except KeyboardInterrupt:
                    console.print("\n[nexus.muted]Stopped watching[/nexus.muted]")
        else:
            # List recent running jobs
            with timing.phase("server"):
                try:
                    jobs = await nx.service("sync_job").list_jobs(status="running", limit=10)
                except AttributeError:
                    console.print(
                        "[nexus.error]Error:[/nexus.error] This Nexus instance doesn't support sync jobs"
                    )
                    sys.exit(1)

            if not jobs:
                console.print("[nexus.warning]No running sync jobs[/nexus.warning]")
                console.print(
                    "[nexus.muted]Use 'nexus mounts sync-jobs' to see all jobs[/nexus.muted]"
                )
                return

            def _render_jobs(data: list[dict[str, Any]]) -> None:
                console.print(
                    f"[bold nexus.value]Running Sync Jobs ({len(data)})[/bold nexus.value]"
                )
                console.print()
                for job_item in data:
                    console.print(f"  [bold]{job_item['id'][:8]}...[/bold]")
                    console.print(f"    Mount: {job_item['mount_point']}")
                    console.print(f"    Progress: {job_item['progress_pct']}%")
                    console.print()

            render_output(
                data=jobs,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_render_jobs,
            )

    except Exception as e:
        handle_error(e)


def _display_job_status(job: dict[str, Any]) -> None:
    """Display job status in a formatted way."""
    status_colors = {
        "pending": "nexus.warning",
        "running": "nexus.value",
        "completed": "nexus.success",
        "failed": "nexus.error",
        "cancelled": "nexus.warning",
    }
    status = job["status"]
    color = status_colors.get(status, "white")

    console.print(f"[bold nexus.value]Sync Job: {job['id']}[/bold nexus.value]")
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
        console.print(f"  [nexus.error]Error: {job['error_message']}[/nexus.error]")

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
@add_output_options
@add_backend_options
def sync_cancel(
    job_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Cancel a running sync job.

    Examples:
        nexus mounts sync-cancel abc123
    """
    import asyncio

    asyncio.run(_async_sync_cancel(job_id, output_opts, remote_url, remote_api_key))


async def _async_sync_cancel(
    job_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)

        with timing.phase("server"):
            try:
                result = await nx.service("sync_job").cancel_sync_job(job_id)
            except AttributeError:
                console.print(
                    "[nexus.error]Error:[/nexus.error] This Nexus instance doesn't support sync jobs"
                )
                sys.exit(1)

        def _render(data: dict[str, Any]) -> None:
            if data["success"]:
                console.print(
                    f"[nexus.success]Cancellation requested for job {job_id}[/nexus.success]"
                )
                console.print("[nexus.muted]Job will stop at next checkpoint[/nexus.muted]")
            else:
                console.print(f"[nexus.error]Failed:[/nexus.error] {data['message']}")
                sys.exit(1)

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

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
@add_output_options
@add_backend_options
def sync_jobs(
    mount: str | None,
    status: str | None,
    limit: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
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
    import asyncio

    asyncio.run(_async_sync_jobs(mount, status, limit, output_opts, remote_url, remote_api_key))


async def _async_sync_jobs(
    mount: str | None,
    status: str | None,
    limit: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)

        with timing.phase("server"):
            try:
                jobs = await nx.service("sync_job").list_jobs(
                    mount_point=mount, status=status, limit=limit
                )
            except AttributeError:
                console.print(
                    "[nexus.error]Error:[/nexus.error] This Nexus instance doesn't support sync jobs"
                )
                sys.exit(1)

        if not jobs:
            console.print("[nexus.warning]No sync jobs found[/nexus.warning]")
            return

        def _render(data: list[dict[str, Any]]) -> None:
            status_colors = {
                "pending": "nexus.warning",
                "running": "nexus.value",
                "completed": "nexus.success",
                "failed": "nexus.error",
                "cancelled": "nexus.warning",
            }

            console.print(f"[bold nexus.value]Sync Jobs ({len(data)} shown)[/bold nexus.value]")
            console.print()

            for job in data:
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
            console.print(
                "[nexus.muted]Use 'nexus mounts sync-status <job_id>' for details[/nexus.muted]"
            )

        render_output(
            data=jobs,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="skills")
@click.argument("mount_point", type=str)
@add_backend_options
@add_output_options
def list_skills(
    mount_point: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    """List available skills for a mounted connector.

    Shows operations, write paths, and traits for a connector.
    Reads from the .skill/ directory at the mount point.

    MOUNT_POINT: Path of the mount (e.g., /mnt/gmail)

    Examples:
        nexus mounts skills /mnt/gmail
        nexus mounts skills /mnt/calendar --format json
    """
    import asyncio

    asyncio.run(_async_list_skills(mount_point, remote_url, remote_api_key, output_opts))


async def _async_list_skills(
    mount_point: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        mp = mount_point.rstrip("/")
        skill_md_path = f"{mp}/.skill/SKILL.md"
        schemas_dir = f"{mp}/.skill/schemas"

        with timing.phase("server"):
            # Read SKILL.md
            try:
                raw = await nx.sys_read(skill_md_path)
                content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            except Exception:
                console.print(
                    f"[nexus.warning]No skill documentation found at {skill_md_path}[/nexus.warning]"
                )
                console.print(
                    "[nexus.muted]This mount may not be a connector with skill docs.[/nexus.muted]"
                )
                return

            # List schema files
            schema_files: list[str] = []
            try:
                entries = await nx.sys_readdir(schemas_dir)
                schema_files = [e.rstrip("/") for e in entries if str(e).endswith(".yaml")]
            except Exception:
                pass  # No schemas directory is OK

        def _render(data: dict[str, Any]) -> None:
            console.print(f"[bold nexus.value]Skills for {mp}[/bold nexus.value]")
            console.print()
            console.print(data["content"])
            if data["schemas"]:
                console.print()
                console.print("[bold]Available Schemas:[/bold]")
                for s in data["schemas"]:
                    op_name = s.replace(".yaml", "")
                    console.print(
                        f"  [nexus.success]{op_name}[/nexus.success]  \u2192  nexus mounts schema {mp} {op_name}"
                    )

        result_data = {"mount_point": mp, "content": content, "schemas": schema_files}

        render_output(
            data=result_data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=lambda d: _render(d),
        )

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="schema")
@click.argument("mount_point", type=str)
@click.argument("operation", type=str)
@add_backend_options
@add_output_options
def show_schema(
    mount_point: str,
    operation: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    """Show annotated schema for a connector operation.

    Displays the full schema with field types, constraints, and
    descriptions for a specific write operation.

    MOUNT_POINT: Path of the mount (e.g., /mnt/gmail)

    OPERATION: Operation name (e.g., send_email, create_event)

    Examples:
        nexus mounts schema /mnt/gmail send_email
        nexus mounts schema /mnt/calendar create_event --format json
    """
    import asyncio

    asyncio.run(_async_show_schema(mount_point, operation, remote_url, remote_api_key, output_opts))


async def _async_show_schema(
    mount_point: str,
    operation: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        mp = mount_point.rstrip("/")
        schema_path = f"{mp}/.skill/schemas/{operation}.yaml"

        with timing.phase("server"):
            try:
                raw = await nx.sys_read(schema_path)
                content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            except Exception:
                console.print(f"[nexus.error]Schema not found:[/nexus.error] {schema_path}")
                console.print()
                console.print("[nexus.muted]Available operations:[/nexus.muted]")
                try:
                    entries = await nx.sys_readdir(f"{mp}/.skill/schemas")
                    for e in entries:
                        if str(e).endswith(".yaml"):
                            console.print(
                                f"  [nexus.success]{str(e).replace('.yaml', '')}[/nexus.success]"
                            )
                except Exception:
                    console.print(
                        "  [nexus.warning]No schemas found for this mount[/nexus.warning]"
                    )
                return

        def _render(data: dict[str, Any]) -> None:
            console.print(f"[bold nexus.value]Schema: {operation}[/bold nexus.value]  ({mp})")
            console.print()
            console.print(data["content"])

        render_output(
            data={"mount_point": mp, "operation": operation, "content": content},
            output_opts=output_opts,
            timing=timing,
            human_formatter=lambda d: _render(d),
        )

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="reauth")
@click.argument("mount_point", type=str)
@click.option("--provider", type=str, default=None, help="OAuth provider name (auto-detected)")
@click.option("--email", type=str, default=None, help="User email for token lookup")
@add_backend_options
@add_output_options
def reauth_mount(
    mount_point: str,
    provider: str | None,
    email: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    """Refresh OAuth credentials for a mounted connector.

    Triggers a token refresh without unmounting. Useful for expired
    tokens or credential rotation.

    MOUNT_POINT: Path of the mount (e.g., /mnt/gmail)

    Examples:
        nexus mounts reauth /mnt/gmail
        nexus mounts reauth /mnt/drive --provider google --email alice@example.com
    """
    import asyncio

    asyncio.run(
        _async_reauth_mount(mount_point, provider, email, remote_url, remote_api_key, output_opts)
    )


async def _async_reauth_mount(
    mount_point: str,
    provider: str | None,
    email: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        mount_svc = nx.service("mount")
        if mount_svc is None:
            console.print("[nexus.error]Error:[/nexus.error] Mount service not available")
            sys.exit(1)

        with timing.phase("server"):
            result = await mount_svc.reauth_mount(
                mount_point=mount_point,
                provider=provider,
                user_email=email,
            )

        def _render(data: dict[str, Any]) -> None:
            if data.get("refreshed"):
                console.print(
                    f"[nexus.success]Token refreshed[/nexus.success] for {mount_point} "
                    f"(provider={data.get('provider')}, user={data.get('user_email')})"
                )
            else:
                console.print(f"[nexus.error]Token refresh failed[/nexus.error] for {mount_point}")
                if data.get("error"):
                    console.print(f"  Error: {data['error']}")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=lambda d: _render(d),
        )

    except Exception as e:
        handle_error(e)


@mounts_group.command(name="update")
@click.argument("mount_point", type=str)
@click.argument("config_json", type=str)
@add_backend_options
@add_output_options
def update_mount(
    mount_point: str,
    config_json: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    """Update mount backend configuration without unmounting.

    Reconfigures the backend (new endpoint, rotated key) while preserving
    permissions, metadata index, and mount state.

    MOUNT_POINT: Path of the mount (e.g., /mnt/gmail)

    CONFIG_JSON: New configuration as JSON string (merged with existing)

    Examples:
        nexus mounts update /mnt/crm '{"api_url": "https://crm-v2.internal"}'
    """
    import asyncio

    asyncio.run(
        _async_update_mount(mount_point, config_json, remote_url, remote_api_key, output_opts)
    )


async def _async_update_mount(
    mount_point: str,
    config_json: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    timing = CommandTiming()
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        mount_svc = nx.service("mount")
        if mount_svc is None:
            console.print("[nexus.error]Error:[/nexus.error] Mount service not available")
            sys.exit(1)

        config = json.loads(config_json)

        with timing.phase("server"):
            result = await mount_svc.update_mount(
                mount_point=mount_point,
                backend_config=config,
            )

        def _render(data: dict[str, Any]) -> None:
            if data.get("updated"):
                console.print(f"[nexus.success]Updated[/nexus.success] {mount_point}")
                console.print(f"  Changed: {', '.join(data.get('changed_keys', []))}")
            else:
                console.print(f"[nexus.warning]No changes[/nexus.warning] for {mount_point}")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=lambda d: _render(d),
        )

    except json.JSONDecodeError as e:
        console.print(f"[nexus.error]Invalid JSON:[/nexus.error] {e}")
        sys.exit(1)
    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register mount commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mounts_group)
