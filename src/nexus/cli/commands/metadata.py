"""Nexus CLI Metadata Commands - File information and metadata operations.

Commands for viewing file information, exporting/importing metadata, and calculating sizes.
"""

import sys
from typing import Any, cast

import click
from rich.table import Table

import nexus
from nexus.cli.formatters import format_timestamp
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
    open_filesystem,
)


@click.command()
@click.argument("path", type=str)
@add_output_options
@add_backend_options
def info(
    path: str,
    output_opts: OutputOptions,
    backend_config: BackendConfig,
) -> None:
    """Show detailed file information.

    Examples:
        nexus info /workspace/data.txt
        nexus info /workspace/data.txt --json
        nexus info /workspace/data.txt --json --fields path,size,etag
    """
    timing = CommandTiming()

    try:
        from nexus.core.nexus_fs import NexusFS

        with timing.phase("connect"), open_filesystem(backend_config) as nx:
            # Check if file exists first
            if not nx.sys_access(path):
                render_output(
                    data=None,
                    output_opts=output_opts,
                    timing=timing,
                    message=f"File not found: {path}",
                )
                sys.exit(1)

            # Note: Only NexusFS mode has direct metadata access
            if not isinstance(nx, NexusFS):
                render_output(
                    data=None,
                    output_opts=output_opts,
                    timing=timing,
                    message="File info is only available for NexusFS instances",
                )
                return

            with timing.phase("server"):
                file_meta = nx.metadata.get(path)

        if not file_meta:
            render_output(
                data=None, output_opts=output_opts, timing=timing, message=f"File not found: {path}"
            )
            sys.exit(1)

        created_str = format_timestamp(file_meta.created_at)
        modified_str = format_timestamp(file_meta.modified_at)

        data = {
            "path": file_meta.path,
            "size": file_meta.size,
            "created_at": created_str,
            "modified_at": modified_str,
            "etag": file_meta.etag or None,
            "mime_type": file_meta.mime_type or None,
        }

        def _print_human(d: dict[str, Any]) -> None:
            table = Table(title=f"File Information: {path}")
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("Path", d["path"])
            table.add_row("Size", f"{d['size']:,} bytes")
            table.add_row("Created", d["created_at"])
            table.add_row("Modified", d["modified_at"])
            table.add_row("ETag", d["etag"] or "N/A")
            table.add_row("MIME Type", d["mime_type"] or "N/A")
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


@click.command()
@add_backend_options
def version(
    backend_config: BackendConfig,
) -> None:  # noqa: ARG001
    """Show Nexus version information."""
    console.print(f"[cyan]Nexus[/cyan] version [green]{nexus.__version__}[/green]")
    console.print(f"Data directory: [cyan]{backend_config.data_dir}[/cyan]")


@click.command(name="export")
@click.argument("output", type=click.Path())
@click.option("-p", "--prefix", default="", help="Export only files with this prefix")
@click.option("--zone-id", default=None, help="Filter by zone ID")
@click.option(
    "--after",
    default=None,
    help="Export only files modified after this time (ISO format: 2024-01-01T00:00:00)",
)
@click.option("--include-deleted", is_flag=True, help="Include soft-deleted files in export")
@add_backend_options
def export_metadata(
    output: str,
    prefix: str,
    zone_id: str | None,
    after: str | None,
    include_deleted: bool,
    backend_config: BackendConfig,
) -> None:
    """Export metadata to JSONL file for backup and migration.

    Exports all file metadata (paths, sizes, timestamps, hashes, custom metadata)
    to a JSONL file. Each line is a JSON object representing one file.

    Output is sorted by path for clean git diffs.

    IMPORTANT: This exports metadata only, not file content. The content remains
    in the CAS storage. To restore, you need both the metadata JSONL file AND
    the CAS storage directory.

    Examples:
        nexus export metadata-backup.jsonl
        nexus export workspace-backup.jsonl --prefix /workspace
        nexus export recent.jsonl --after 2024-01-01T00:00:00
        nexus export zone.jsonl --zone-id acme-corp
    """
    try:
        from nexus.core.nexus_fs import NexusFS
        from nexus.lib.export_import import ExportFilter

        nx_raw: Any = get_filesystem(backend_config)

        # Note: Only standalone mode supports metadata export
        if not isinstance(nx_raw, NexusFS):
            console.print("[red]Error:[/red] Metadata export is only available in standalone mode")
            nx_raw.close()
            sys.exit(1)

        nx: Any = nx_raw  # keep as Any to access DI service slots

        # Parse after time if provided
        after_time = None
        if after:
            from datetime import datetime

            try:
                after_time = datetime.fromisoformat(after)
            except ValueError:
                console.print(
                    f"[red]Error:[/red] Invalid date format: {after}. Use ISO format (2024-01-01T00:00:00)"
                )
                nx.close()
                sys.exit(1)

        # Create export filter
        export_filter = ExportFilter(
            zone_id=zone_id,
            path_prefix=prefix,
            after_time=after_time,
            include_deleted=include_deleted,
        )

        # Display filter options
        console.print(f"[cyan]Exporting metadata to:[/cyan] {output}")
        if prefix:
            console.print(f"  Path prefix: [cyan]{prefix}[/cyan]")
        if zone_id:
            console.print(f"  Zone ID: [cyan]{zone_id}[/cyan]")
        if after_time:
            console.print(f"  After time: [cyan]{after_time.isoformat()}[/cyan]")
        if include_deleted:
            console.print("  [yellow]Including deleted files[/yellow]")

        with console.status("[yellow]Exporting metadata...[/yellow]", spinner="dots"):
            count = nx._metadata_export_service.export_metadata(output, filter=export_filter)

        nx.close()

        console.print(f"[green]✓[/green] Exported [cyan]{count}[/cyan] file metadata records")
        console.print(f"  Output: [cyan]{output}[/cyan]")
    except Exception as e:
        handle_error(e)


@click.command(name="import")
@click.argument("input_file", type=click.Path(exists=True))
@click.option(
    "--conflict-mode",
    type=click.Choice(["skip", "overwrite", "remap", "auto"]),
    default="skip",
    help="How to handle path collisions (default: skip)",
)
@click.option("--dry-run", is_flag=True, help="Simulate import without making changes")
@click.option(
    "--no-preserve-ids",
    is_flag=True,
    help="Don't preserve original UUIDs from export (default: preserve)",
)
@add_backend_options
def import_metadata(
    input_file: str,
    conflict_mode: str,
    dry_run: bool,
    no_preserve_ids: bool,
    backend_config: BackendConfig,
) -> None:
    """Import metadata from JSONL file.

    IMPORTANT: This imports metadata only, not file content. The content must
    already exist in the CAS storage (matched by content hash). This is useful for:
    - Restoring metadata after database corruption
    - Migrating metadata between instances (with same CAS content)
    - Creating alternative path mappings to existing content

    Conflict Resolution Modes:
    - skip: Keep existing files, skip imports (default)
    - overwrite: Replace existing files with imported data
    - remap: Rename imported files to avoid collisions (adds _imported suffix)
    - auto: Smart resolution - newer file wins based on timestamps

    Examples:
        nexus import metadata-backup.jsonl
        nexus import metadata-backup.jsonl --conflict-mode=overwrite
        nexus import metadata-backup.jsonl --conflict-mode=auto --dry-run
        nexus import metadata-backup.jsonl --conflict-mode=remap
    """
    try:
        from nexus.core.nexus_fs import NexusFS
        from nexus.lib.export_import import ConflictMode, ImportOptions

        nx_raw: Any = get_filesystem(backend_config)

        # Note: Only standalone mode supports metadata import
        if not isinstance(nx_raw, NexusFS):
            console.print("[red]Error:[/red] Metadata import is only available in standalone mode")
            nx_raw.close()
            sys.exit(1)

        nx: Any = nx_raw  # keep as Any to access DI service slots

        # Create import options
        import_options = ImportOptions(
            dry_run=dry_run,
            conflict_mode=cast(ConflictMode, conflict_mode),
            preserve_ids=not no_preserve_ids,
        )

        # Display import configuration
        console.print(f"[cyan]Importing metadata from:[/cyan] {input_file}")
        console.print(f"  Conflict mode: [yellow]{conflict_mode}[/yellow]")
        if dry_run:
            console.print("  [yellow]DRY RUN - No changes will be made[/yellow]")
        if no_preserve_ids:
            console.print("  [yellow]Not preserving original IDs[/yellow]")

        with console.status("[yellow]Importing metadata...[/yellow]", spinner="dots"):
            result = nx._metadata_export_service.import_metadata(input_file, options=import_options)

        nx.close()

        # Display results
        if dry_run:
            console.print("[bold yellow]DRY RUN RESULTS:[/bold yellow]")
        else:
            console.print("[bold green]✓ Import Complete![/bold green]")

        console.print(f"  Created: [green]{result.created}[/green]")
        console.print(f"  Updated: [cyan]{result.updated}[/cyan]")
        console.print(f"  Skipped: [yellow]{result.skipped}[/yellow]")
        if result.remapped > 0:
            console.print(f"  Remapped: [magenta]{result.remapped}[/magenta]")
        console.print(f"  Total: [bold]{result.total_processed}[/bold]")

        # Display collisions if any
        if result.collisions:
            console.print(f"\n[bold yellow]Collisions:[/bold yellow] {len(result.collisions)}")
            console.print()

            # Group collisions by resolution type
            from collections import defaultdict

            by_resolution = defaultdict(list)
            for collision in result.collisions:
                by_resolution[collision.resolution].append(collision)

            # Show summary by resolution type
            for resolution, collisions in sorted(by_resolution.items()):
                console.print(f"  [cyan]{resolution}:[/cyan] {len(collisions)} files")

            # Show detailed collision list (limit to first 10 for readability)
            if len(result.collisions) <= 10:
                console.print("\n[bold]Collision Details:[/bold]")
                for collision in result.collisions:
                    console.print(f"  • {collision.path}")
                    console.print(f"    [dim]{collision.message}[/dim]")
            else:
                console.print("\n[dim]Use --dry-run to see all collision details[/dim]")

    except Exception as e:
        handle_error(e)


@click.command(name="size")
@click.argument("path", default="/", type=str)
@click.option("--human", "-h", is_flag=True, help="Human-readable output")
@click.option("--details", is_flag=True, help="Show per-file breakdown")
@add_backend_options
def size(
    path: str,
    human: bool,
    details: bool,
    backend_config: BackendConfig,
) -> None:
    """Calculate total size of files in a path.

    Recursively calculates the total size of all files under a given path.

    Examples:
        nexus size /workspace
        nexus size /workspace --human
        nexus size /workspace --details
    """
    try:
        nx = get_filesystem(backend_config)

        # Get all files with details
        with console.status(f"[yellow]Calculating size of {path}...[/yellow]", spinner="dots"):
            files_raw = nx.sys_readdir(path, recursive=True, details=True)

        nx.close()

        if not files_raw:
            console.print(f"[yellow]No files found in {path}[/yellow]")
            return

        files = cast(list[dict[str, Any]], files_raw)

        # Calculate total size
        total_size = sum(f["size"] for f in files)
        file_count = len(files)

        def format_size(size: int) -> str:
            """Format size in human-readable format."""
            if not human:
                return f"{size:,} bytes"

            size_float = float(size)
            for unit in ["B", "KB", "MB", "GB", "TB"]:
                if size_float < 1024.0:
                    return f"{size_float:.1f} {unit}"
                size_float /= 1024.0
            return f"{size_float:.1f} PB"

        # Display summary
        console.print(f"[bold cyan]Size of {path}:[/bold cyan]")
        console.print(f"  Total size: [green]{format_size(total_size)}[/green]")
        console.print(f"  File count: [cyan]{file_count:,}[/cyan]")

        if details:
            console.print()
            console.print("[bold]Top 10 largest files:[/bold]")

            # Sort by size and show top 10
            sorted_files = sorted(files, key=lambda f: f["size"], reverse=True)[:10]

            table = Table()
            table.add_column("Size", justify="right", style="green")
            table.add_column("Path", style="cyan")

            for file in sorted_files:
                table.add_row(format_size(file["size"]), file["path"])

            console.print(table)

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register all metadata commands to the CLI group.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(info)
    cli.add_command(version)
    cli.add_command(export_metadata)
    cli.add_command(import_metadata)
    cli.add_command(size)
