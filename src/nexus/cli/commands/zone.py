"""Nexus CLI Zone Commands.

Subcommands:
  Federation (Issue #1326):
    zone create   - Create a new Raft zone
    zone join     - Join an existing zone as Voter
    zone list     - List local zones
    zone mount    - Mount a zone at a path (DT_MOUNT)
    zone unmount  - Remove a mount point

  Portability (Issue #1161):
    zone export   - Export zone data to .nexus bundle
    zone import   - Import zone data from .nexus bundle
    zone inspect  - Inspect a .nexus bundle
    zone validate - Validate a .nexus bundle
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from nexus.cli.dry_run import add_dry_run_option, dry_run_preview, render_dry_run
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
    rpc_call,
)
from nexus.contracts.constants import DEFAULT_GRPC_BIND_ADDR


@click.group()
def zone() -> None:
    """Zone management — federation and portability.

    Federation commands (create, join, list, mount, unmount) manage
    Raft zones and cross-zone DT_MOUNT points. Requires PyO3 build
    with --features full.

    Portability commands (export, import, inspect, validate) work
    with .nexus bundle files for zone data migration.
    """
    pass


# =========================================================================
# Federation commands (Issue #1326)
# =========================================================================


def _get_zone_manager(
    hostname: str,
    data_dir: str,
    bind_addr: str,
    peers: list[str] | None = None,
) -> Any:
    """Create a ZoneManager from CLI options.

    R20.18.5: Python ZoneManager was deleted — federation is kernel-
    internal now. CLI commands that relied on a direct-open ZoneManager
    (create / join / mount / unmount) now raise a clear error; callers
    should drive zone CRUD via the RPC path through a running server
    process. Export/import flows (R20.17) will reach raft-backed zones
    through the same RPC surface.
    """
    del hostname, data_dir, bind_addr, peers
    raise RuntimeError(
        "Direct-open ZoneManager is no longer available — the Python "
        "shim was deleted in R20.18.5. Drive zone CRUD through the "
        "nexus RPC surface (a running nexus-server) instead."
    )


@zone.command(name="create")
@click.argument("zone_id", type=str)
@click.option(
    "--hostname",
    type=str,
    envvar="NEXUS_HOSTNAME",
    default=None,
    help="This node's hostname (default: socket.gethostname())",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    envvar="NEXUS_DATA_DIR",
    default="./nexus-data/zones",
    show_default=True,
    help="Base directory for zone databases",
)
@click.option(
    "--bind",
    type=str,
    envvar="NEXUS_BIND_ADDR",
    default=DEFAULT_GRPC_BIND_ADDR,
    show_default=True,
    help="gRPC bind address",
)
@click.option(
    "--peers",
    type=str,
    default=None,
    help="Comma-separated peer addresses (format: host:port)",
)
@click.option(
    "--if-not-exists",
    is_flag=True,
    default=False,
    help="Succeed silently if zone already exists",
)
@add_dry_run_option
def create_zone_cmd(
    zone_id: str,
    hostname: str | None,
    data_dir: str,
    bind: str,
    peers: str | None,
    if_not_exists: bool,
    dry_run: bool,
) -> None:
    """Create a new Raft zone.

    Each zone is an independent Raft group with its own redb database.
    All participants are equal Voters (All-Voters model).

    Examples:
        nexus zone create my-zone

        nexus zone create shared-zone --peers peer2:2126,peer3:2126

        nexus zone create my-zone --hostname nexus-1

        nexus zone create my-zone --dry-run

        nexus zone create my-zone --if-not-exists
    """
    import socket

    if hostname is None:
        hostname = socket.gethostname()

    try:
        if dry_run:
            preview = dry_run_preview(
                "zone create", path=zone_id, details={"hostname": hostname, "peers": peers}
            )
            render_dry_run(preview)
            return

        peer_list = [p.strip() for p in peers.split(",")] if peers else []
        mgr = _get_zone_manager(hostname, data_dir, bind, peers=peer_list)

        try:
            store = mgr.create_zone(zone_id, peers=peer_list)
        except Exception as create_err:
            if if_not_exists and "already exists" in str(create_err).lower():
                console.print(f"[nexus.success]✓[/nexus.success] Zone already exists: {zone_id}")
                mgr.shutdown()
                return
            raise

        console.print(f"[nexus.success]Zone '{zone_id}' created[/nexus.success]")
        console.print(f"  Hostname: {hostname}")
        console.print(f"  Data dir: {data_dir}/{zone_id}/")
        console.print(f"  Bind: {bind}")
        if peer_list:
            console.print(f"  Peers: {', '.join(peer_list)}")

        store.close()
        mgr.shutdown()
    except Exception as e:
        handle_error(e)


@zone.command(name="join")
@click.argument("zone_id", type=str)
@click.option(
    "--hostname",
    type=str,
    envvar="NEXUS_HOSTNAME",
    default=None,
    help="This node's hostname (default: socket.gethostname())",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    envvar="NEXUS_DATA_DIR",
    default="./nexus-data/zones",
    show_default=True,
    help="Base directory for zone databases",
)
@click.option(
    "--bind",
    type=str,
    envvar="NEXUS_BIND_ADDR",
    default=DEFAULT_GRPC_BIND_ADDR,
    show_default=True,
    help="gRPC bind address",
)
@click.option(
    "--peers",
    type=str,
    required=True,
    help="Comma-separated existing peer addresses (format: host:port)",
)
def join_zone_cmd(
    zone_id: str,
    hostname: str | None,
    data_dir: str,
    bind: str,
    peers: str,
) -> None:
    """Join an existing zone as a new Voter.

    Creates a local RaftNode without bootstrapping. The leader must
    be notified via JoinZone RPC to add this node via ConfChange.

    Examples:
        nexus zone join shared-zone --peers leader:2126,peer2:2126
    """
    import socket

    if hostname is None:
        hostname = socket.gethostname()

    try:
        peer_list = [p.strip() for p in peers.split(",")]
        mgr = _get_zone_manager(hostname, data_dir, bind, peers=peer_list)
        store = mgr.join_zone(zone_id, peers=peer_list)

        console.print(f"[nexus.success]Joined zone '{zone_id}'[/nexus.success]")
        console.print(f"  Hostname: {hostname}")
        console.print(f"  Peers: {', '.join(peer_list)}")
        console.print("  Waiting for leader to send snapshot...")

        store.close()
        mgr.shutdown()
    except Exception as e:
        handle_error(e)


@zone.command(name="list")
@click.option(
    "--hostname",
    type=str,
    envvar="NEXUS_HOSTNAME",
    default=None,
    help="This node's hostname (default: socket.gethostname())",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    envvar="NEXUS_DATA_DIR",
    default="./nexus-data/zones",
    show_default=True,
    help="Base directory for zone databases",
)
@click.option(
    "--bind",
    type=str,
    envvar="NEXUS_BIND_ADDR",
    default=DEFAULT_GRPC_BIND_ADDR,
    show_default=True,
    help="gRPC bind address",
)
@REMOTE_URL_OPTION
@REMOTE_API_KEY_OPTION
@add_output_options
def list_zones_cmd(
    hostname: str | None,
    data_dir: str,
    bind: str,
    remote_url: str | None,
    remote_api_key: str | None,
    output_opts: OutputOptions,
) -> None:
    """List all local zones on this node.

    Examples:
        nexus zone list
        nexus zone list --json
        nexus zone list --data-dir /var/lib/nexus/zones
    """
    import socket

    if hostname is None:
        hostname = socket.gethostname()

    timing = CommandTiming()

    try:
        # When NEXUS_URL / --remote-url is set we dispatch through the running
        # server — this avoids fighting for the redb lock when a node is live.
        # Kernel-state is owned by the server process; CLI is a thin user-space
        # tool that reads via RPC. Only fall back to direct ZoneManager open
        # for maintenance scenarios (no server running).
        if remote_url:
            with timing.phase("server"):
                rpc_data = rpc_call(remote_url, remote_api_key, "federation_list_zones")
            zones = [z.get("zone_id", "") for z in rpc_data.get("zones", [])]
        else:
            with timing.phase("server"):
                mgr = _get_zone_manager(hostname, data_dir, bind)
                zones = mgr.list_zones()
                mgr.shutdown()

        data = [{"zone_id": z} for z in sorted(zones)] if zones else []

        def _print_human(entries: list[dict[str, Any]]) -> None:
            if not entries:
                console.print("[nexus.muted]No zones found[/nexus.muted]")
                return
            table = Table(title=f"Zones ({hostname})")
            table.add_column("Zone ID", style="nexus.value")
            console.print()
            for entry in entries:
                table.add_row(entry["zone_id"])
            console.print(table)

        render_output(
            data=data, output_opts=output_opts, timing=timing, human_formatter=_print_human
        )
    except Exception as e:
        if output_opts.json_output:
            from nexus.cli.exit_codes import ExitCode

            render_error(
                error=e, output_opts=output_opts, exit_code=ExitCode.GENERAL_ERROR, timing=timing
            )
        else:
            handle_error(e)


@zone.command(name="mount")
@click.argument("mount_path", type=str)
@click.argument("target_zone", type=str)
@click.option(
    "--parent-zone",
    type=str,
    default="default",
    show_default=True,
    help="Zone containing the mount point",
)
@click.option(
    "--hostname",
    type=str,
    envvar="NEXUS_HOSTNAME",
    default=None,
    help="This node's hostname (default: socket.gethostname())",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    envvar="NEXUS_DATA_DIR",
    default="./nexus-data/zones",
    show_default=True,
    help="Base directory for zone databases",
)
@click.option(
    "--bind",
    type=str,
    envvar="NEXUS_BIND_ADDR",
    default=DEFAULT_GRPC_BIND_ADDR,
    show_default=True,
    help="gRPC bind address",
)
@add_dry_run_option
def mount_zone_cmd(
    mount_path: str,
    target_zone: str,
    parent_zone: str,
    hostname: str | None,
    data_dir: str,
    bind: str,
    dry_run: bool,
) -> None:
    """Mount a zone at a path (DT_MOUNT).

    Creates a cross-zone mount point. Files under MOUNT_PATH will
    be routed to TARGET_ZONE's metadata store.

    NFS-style semantics: rejects if MOUNT_PATH already exists (no shadow).

    Examples:
        nexus zone mount /shared team-zone

        nexus zone mount /projects/alice alice-zone --parent-zone root

        nexus zone mount /shared team-zone --dry-run
    """
    import socket

    if hostname is None:
        hostname = socket.gethostname()

    try:
        if dry_run:
            preview = dry_run_preview(
                "zone mount",
                path=mount_path,
                details={"target_zone": target_zone, "parent_zone": parent_zone},
            )
            render_dry_run(preview)
            return

        mgr = _get_zone_manager(hostname, data_dir, bind)
        mgr.mount(parent_zone, mount_path, target_zone)

        console.print(
            f"[nexus.success]Mounted zone '{target_zone}' at '{mount_path}' in zone '{parent_zone}'[/nexus.success]"
        )

        mgr.shutdown()
    except Exception as e:
        handle_error(e)


@zone.command(name="unmount")
@click.argument("mount_path", type=str)
@click.option(
    "--parent-zone",
    type=str,
    default="default",
    show_default=True,
    help="Zone containing the mount point",
)
@click.option(
    "--hostname",
    type=str,
    envvar="NEXUS_HOSTNAME",
    default=None,
    help="This node's hostname (default: socket.gethostname())",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    envvar="NEXUS_DATA_DIR",
    default="./nexus-data/zones",
    show_default=True,
    help="Base directory for zone databases",
)
@click.option(
    "--bind",
    type=str,
    envvar="NEXUS_BIND_ADDR",
    default=DEFAULT_GRPC_BIND_ADDR,
    show_default=True,
    help="gRPC bind address",
)
@add_dry_run_option
def unmount_zone_cmd(
    mount_path: str,
    parent_zone: str,
    hostname: str | None,
    data_dir: str,
    bind: str,
    dry_run: bool,
) -> None:
    """Remove a mount point (DT_MOUNT).

    Examples:
        nexus zone unmount /shared

        nexus zone unmount /projects/alice --parent-zone root

        nexus zone unmount /shared --dry-run
    """
    import socket

    if hostname is None:
        hostname = socket.gethostname()

    try:
        if dry_run:
            preview = dry_run_preview(
                "zone unmount",
                path=mount_path,
                details={"parent_zone": parent_zone},
            )
            render_dry_run(preview)
            return

        mgr = _get_zone_manager(hostname, data_dir, bind)
        mgr.unmount(parent_zone, mount_path)

        console.print(
            f"[nexus.success]Unmounted '{mount_path}' from zone '{parent_zone}'[/nexus.success]"
        )

        mgr.shutdown()
    except Exception as e:
        handle_error(e)


@zone.command(name="export")
@click.argument("zone_id", type=str)
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(dir_okay=False),
    help="Output path for .nexus bundle",
)
@click.option(
    "--include-content/--no-content",
    default=True,
    help="Include file content blobs (default: yes)",
)
@click.option(
    "--include-permissions/--no-permissions",
    default=True,
    help="Include ReBAC permissions (default: yes)",
)
@click.option(
    "--include-embeddings/--no-embeddings",
    default=False,
    help="Include vector embeddings (default: no)",
)
@click.option(
    "--include-deleted/--no-deleted",
    default=False,
    help="Include soft-deleted files (default: no)",
)
@click.option(
    "-p",
    "--path-prefix",
    default=None,
    help="Only export paths starting with this prefix",
)
@click.option(
    "--after",
    default=None,
    help="Only export files modified after this time (ISO format: 2025-01-01T00:00:00)",
)
@click.option(
    "--compression",
    type=click.IntRange(1, 9),
    default=6,
    help="Compression level 1-9 (default: 6)",
)
@add_backend_options
def export_zone(
    zone_id: str,
    output: str,
    include_content: bool,
    include_permissions: bool,
    include_embeddings: bool,
    include_deleted: bool,
    path_prefix: str | None,
    after: str | None,
    compression: int,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Export zone data to a portable .nexus bundle.

    Creates a complete export of zone data including:
    - File metadata (paths, timestamps, versions)
    - Content blobs (actual file data)
    - Permissions (ReBAC tuples)
    - Embeddings (optional)

    Examples:
        nexus zone export acme-corp -o /backup/acme.nexus

        nexus zone export acme-corp -o /backup/acme.nexus --path-prefix /workspace/

        nexus zone export acme-corp -o /backup/acme.nexus --after 2025-01-01T00:00:00
    """
    try:
        from nexus.bricks.portability import ZoneExportOptions, ZoneExportService

        # Parse after time if provided
        after_time = None
        if after:
            try:
                after_time = datetime.fromisoformat(after)
                if after_time.tzinfo is None:
                    after_time = after_time.replace(tzinfo=UTC)
            except ValueError:
                console.print(f"[nexus.error]Error:[/nexus.error] Invalid date format: {after}")
                console.print("Use ISO format: 2025-01-01T00:00:00")
                sys.exit(1)

        # Get filesystem (get_filesystem is async — CLI needs a sync bridge)
        import asyncio as _asyncio

        nx: Any = _asyncio.run(get_filesystem(remote_url, remote_api_key))

        # Configure export options
        output_path = Path(output)
        if not str(output_path).endswith(".nexus"):
            output_path = output_path.with_suffix(".nexus")

        options = ZoneExportOptions(
            output_path=output_path,
            include_content=include_content,
            include_permissions=include_permissions,
            include_embeddings=include_embeddings,
            include_deleted=include_deleted,
            path_prefix=path_prefix,
            after_time=after_time,
            compression_level=compression,
        )

        # Run export with progress
        console.print(f"[nexus.value]Exporting zone:[/nexus.value] {zone_id}")
        console.print(f"[nexus.path]Output:[/nexus.path] {output_path}")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Exporting...", total=None)

            def update_progress(current: int, total: int) -> None:
                progress.update(task, description=f"Exporting... ({current}/{total} files)")

            service = ZoneExportService(cast(Any, nx))
            manifest = service.export_zone(zone_id, options, update_progress)

        nx.close()

        # Show results
        console.print()
        table = Table(title="Export Complete")
        table.add_column("Metric", style="nexus.value")
        table.add_column("Value", style="nexus.success")

        table.add_row("Files exported", f"{manifest.file_count:,}")
        table.add_row("Total size", f"{manifest.total_size_bytes:,} bytes")
        table.add_row("Content blobs", f"{manifest.content_blob_count:,}")
        table.add_row("Permissions", f"{manifest.permission_count:,}")
        table.add_row("Bundle ID", manifest.bundle_id[:8] + "...")
        table.add_row("Output", str(output_path))

        if output_path.exists():
            table.add_row("Bundle size", f"{output_path.stat().st_size:,} bytes")

        console.print(table)

    except Exception as e:
        handle_error(e)


@zone.command(name="import")
@click.argument("bundle_path", type=click.Path(exists=True))
@click.option(
    "-t",
    "--target-zone",
    default=None,
    help="Remap to different zone ID (default: preserve original)",
)
@click.option(
    "--conflict",
    type=click.Choice(["skip", "overwrite", "merge", "fail"]),
    default="skip",
    help="How to handle existing files (default: skip)",
)
@click.option(
    "--preserve-timestamps/--no-timestamps",
    default=True,
    help="Preserve original timestamps (default: yes)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without applying",
)
@click.option(
    "--import-permissions/--no-permissions",
    default=True,
    help="Import ReBAC permissions (default: yes)",
)
@click.option(
    "--path-remap",
    multiple=True,
    help="Path prefix remapping (format: old=new), can be repeated",
)
@add_backend_options
def import_zone(
    bundle_path: str,
    target_zone: str | None,
    conflict: str,
    preserve_timestamps: bool,
    dry_run: bool,
    import_permissions: bool,
    path_remap: tuple[str, ...],
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Import zone data from a .nexus bundle.

    Restores zone data including:
    - File metadata (paths, timestamps, versions)
    - Content blobs (actual file data)
    - Permissions (ReBAC tuples)

    Examples:
        nexus zone import /backup/acme.nexus

        nexus zone import /backup/acme.nexus --target-zone new-acme

        nexus zone import /backup/acme.nexus --conflict overwrite

        nexus zone import /backup/acme.nexus --path-remap /old/=/new/

        nexus zone import /backup/acme.nexus --dry-run
    """
    try:
        from nexus.bricks.portability import ConflictMode, ZoneImportOptions, ZoneImportService

        # Parse path remappings
        path_prefix_remap: dict[str, str] = {}
        for remap in path_remap:
            if "=" not in remap:
                console.print(
                    f"[nexus.error]Error:[/nexus.error] Invalid path remap format: {remap}"
                )
                console.print("Use format: old=new (e.g., --path-remap /old/=/new/)")
                sys.exit(1)
            old, new = remap.split("=", 1)
            path_prefix_remap[old] = new

        # Get filesystem (get_filesystem is async — CLI needs a sync bridge)
        import asyncio as _asyncio

        nx: Any = _asyncio.run(get_filesystem(remote_url, remote_api_key))

        # Configure import options
        conflict_mode = ConflictMode(conflict)
        options = ZoneImportOptions(
            bundle_path=Path(bundle_path),
            target_zone_id=target_zone,
            conflict_mode=conflict_mode,
            preserve_timestamps=preserve_timestamps,
            dry_run=dry_run,
            import_permissions=import_permissions,
            path_prefix_remap=path_prefix_remap,
        )

        # Show import configuration
        console.print(f"[nexus.path]Importing from:[/nexus.path] {bundle_path}")
        if target_zone:
            console.print(f"[nexus.value]Target zone:[/nexus.value] {target_zone}")
        console.print(f"[nexus.value]Conflict mode:[/nexus.value] {conflict}")
        if dry_run:
            console.print("[nexus.warning]DRY RUN - no changes will be made[/nexus.warning]")

        # Run import with progress
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Importing...", total=None)

            def update_progress(current: int, total: int, phase: str) -> None:
                progress.update(task, description=f"Importing {phase}... ({current}/{total})")

            service = ZoneImportService(cast(Any, nx))
            result = service.import_zone(options, update_progress)

        nx.close()

        # Show results
        console.print()
        table = Table(title="Import Complete" if result.success else "Import Failed")
        table.add_column("Metric", style="nexus.value")
        table.add_column("Value", style="nexus.success" if result.success else "nexus.error")

        table.add_row("Files created", f"{result.files_created:,}")
        table.add_row("Files updated", f"{result.files_updated:,}")
        table.add_row("Files skipped", f"{result.files_skipped:,}")
        table.add_row("Files failed", f"{result.files_failed:,}")
        table.add_row("", "")
        table.add_row("Content blobs imported", f"{result.content_blobs_imported:,}")
        table.add_row("Content blobs skipped", f"{result.content_blobs_skipped:,}")
        table.add_row("", "")
        table.add_row("Permissions imported", f"{result.permissions_imported:,}")
        table.add_row("Paths remapped", f"{result.paths_remapped:,}")
        table.add_row("", "")
        table.add_row("Duration", f"{result.duration_seconds:.2f}s")

        console.print(table)

        # Show errors if any
        if result.errors:
            console.print()
            console.print("[nexus.error]Errors:[/nexus.error]")
            for error in result.errors[:10]:  # Show first 10 errors
                console.print(f"  - {error.path}: {error.message}")
            if len(result.errors) > 10:
                console.print(f"  ... and {len(result.errors) - 10} more errors")
            sys.exit(1)

        # Show warnings if any
        if result.warnings:
            console.print()
            console.print("[nexus.warning]Warnings:[/nexus.warning]")
            for warning in result.warnings[:5]:
                console.print(f"  - {warning}")
            if len(result.warnings) > 5:
                console.print(f"  ... and {len(result.warnings) - 5} more warnings")

        if result.success:
            console.print()
            console.print("[nexus.success]✓ Import completed successfully[/nexus.success]")

    except Exception as e:
        handle_error(e)


@zone.command(name="inspect")
@click.argument("bundle_path", type=click.Path(exists=True))
def inspect_bundle_cmd(bundle_path: str) -> None:
    """Inspect a .nexus bundle and show its contents.

    Examples:
        nexus zone inspect /backup/acme.nexus
    """
    try:
        from nexus.bricks.portability import inspect_bundle

        info = inspect_bundle(bundle_path)

        table = Table(title=f"Bundle: {Path(bundle_path).name}")
        table.add_column("Property", style="nexus.value")
        table.add_column("Value", style="nexus.success")

        table.add_row("Bundle ID", info["bundle_id"][:8] + "...")
        table.add_row("Format Version", info["format_version"])
        table.add_row("Nexus Version", info["nexus_version"])
        table.add_row("Source Zone", info["source_zone_id"])
        table.add_row("Source Instance", info["source_instance"])
        table.add_row("Export Time", info["export_timestamp"])
        table.add_row("", "")
        table.add_row("File Count", f"{info['file_count']:,}")
        table.add_row("Total Size", f"{info['total_size_bytes']:,} bytes")
        table.add_row("Content Blobs", f"{info['content_blob_count']:,}")
        table.add_row("Permissions", f"{info['permission_count']:,}")
        table.add_row("", "")
        table.add_row("Include Content", str(info["include_content"]))
        table.add_row("Include Permissions", str(info["include_permissions"]))
        table.add_row("Include Embeddings", str(info["include_embeddings"]))
        table.add_row("", "")
        table.add_row("Files in Bundle", f"{info['bundle_files']:,}")

        console.print(table)

    except Exception as e:
        handle_error(e)


@zone.command(name="validate")
@click.argument("bundle_path", type=click.Path(exists=True))
def validate_bundle_cmd(bundle_path: str) -> None:
    """Validate a .nexus bundle integrity.

    Checks:
    - Manifest exists and is valid
    - Required files are present
    - Checksums match

    Examples:
        nexus zone validate /backup/acme.nexus
    """
    try:
        from nexus.bricks.portability import validate_bundle

        console.print(f"[nexus.path]Validating:[/nexus.path] {bundle_path}")

        is_valid, errors = validate_bundle(bundle_path)

        if is_valid:
            console.print("[nexus.success]✓ Bundle is valid[/nexus.success]")
        else:
            console.print("[nexus.error]✗ Bundle validation failed:[/nexus.error]")
            for error in errors:
                console.print(f"  - {error}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)
