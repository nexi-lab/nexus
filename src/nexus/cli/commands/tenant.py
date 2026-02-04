"""Nexus CLI Tenant Commands - Tenant export/import operations.

Commands for exporting and importing tenant data as portable .nexus bundles.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)
from nexus.core.nexus_fs import NexusFS


@click.group()
def tenant() -> None:
    """Tenant data management commands.

    Export and import tenant data as portable .nexus bundles.
    """
    pass


@tenant.command(name="export")
@click.argument("tenant_id", type=str)
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
def export_tenant(
    tenant_id: str,
    output: str,
    include_content: bool,
    include_permissions: bool,
    include_embeddings: bool,
    include_deleted: bool,
    path_prefix: str | None,
    after: str | None,
    compression: int,
    backend_config: BackendConfig,
) -> None:
    """Export tenant data to a portable .nexus bundle.

    Creates a complete export of tenant data including:
    - File metadata (paths, timestamps, versions)
    - Content blobs (actual file data)
    - Permissions (ReBAC tuples)
    - Embeddings (optional)

    Examples:
        nexus tenant export acme-corp -o /backup/acme.nexus

        nexus tenant export acme-corp -o /backup/acme.nexus --path-prefix /workspace/

        nexus tenant export acme-corp -o /backup/acme.nexus --after 2025-01-01T00:00:00
    """
    try:
        from nexus.portability import TenantExportOptions, TenantExportService

        # Parse after time if provided
        after_time = None
        if after:
            try:
                after_time = datetime.fromisoformat(after)
                if after_time.tzinfo is None:
                    after_time = after_time.replace(tzinfo=UTC)
            except ValueError:
                console.print(f"[red]Error:[/red] Invalid date format: {after}")
                console.print("Use ISO format: 2025-01-01T00:00:00")
                sys.exit(1)

        # Get filesystem
        nx = get_filesystem(backend_config)
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] Tenant export requires NexusFS instance")
            nx.close()
            sys.exit(1)

        # Configure export options
        output_path = Path(output)
        if not str(output_path).endswith(".nexus"):
            output_path = output_path.with_suffix(".nexus")

        options = TenantExportOptions(
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
        console.print(f"[cyan]Exporting tenant:[/cyan] {tenant_id}")
        console.print(f"[cyan]Output:[/cyan] {output_path}")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Exporting...", total=None)

            def update_progress(current: int, total: int) -> None:
                progress.update(task, description=f"Exporting... ({current}/{total} files)")

            service = TenantExportService(nx)
            manifest = service.export_tenant(tenant_id, options, update_progress)

        nx.close()

        # Show results
        console.print()
        table = Table(title="Export Complete")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

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


@tenant.command(name="import")
@click.argument("bundle_path", type=click.Path(exists=True))
@click.option(
    "-t",
    "--target-tenant",
    default=None,
    help="Remap to different tenant ID (default: preserve original)",
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
def import_tenant(
    bundle_path: str,
    target_tenant: str | None,
    conflict: str,
    preserve_timestamps: bool,
    dry_run: bool,
    import_permissions: bool,
    path_remap: tuple[str, ...],
    backend_config: BackendConfig,
) -> None:
    """Import tenant data from a .nexus bundle.

    Restores tenant data including:
    - File metadata (paths, timestamps, versions)
    - Content blobs (actual file data)
    - Permissions (ReBAC tuples)

    Examples:
        nexus tenant import /backup/acme.nexus

        nexus tenant import /backup/acme.nexus --target-tenant new-acme

        nexus tenant import /backup/acme.nexus --conflict overwrite

        nexus tenant import /backup/acme.nexus --path-remap /old/=/new/

        nexus tenant import /backup/acme.nexus --dry-run
    """
    try:
        from nexus.portability import ConflictMode, TenantImportOptions, TenantImportService

        # Parse path remappings
        path_prefix_remap: dict[str, str] = {}
        for remap in path_remap:
            if "=" not in remap:
                console.print(f"[red]Error:[/red] Invalid path remap format: {remap}")
                console.print("Use format: old=new (e.g., --path-remap /old/=/new/)")
                sys.exit(1)
            old, new = remap.split("=", 1)
            path_prefix_remap[old] = new

        # Get filesystem
        nx = get_filesystem(backend_config)
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] Tenant import requires NexusFS instance")
            nx.close()
            sys.exit(1)

        # Configure import options
        conflict_mode = ConflictMode(conflict)
        options = TenantImportOptions(
            bundle_path=Path(bundle_path),
            target_tenant_id=target_tenant,
            conflict_mode=conflict_mode,
            preserve_timestamps=preserve_timestamps,
            dry_run=dry_run,
            import_permissions=import_permissions,
            path_prefix_remap=path_prefix_remap,
        )

        # Show import configuration
        console.print(f"[cyan]Importing from:[/cyan] {bundle_path}")
        if target_tenant:
            console.print(f"[cyan]Target tenant:[/cyan] {target_tenant}")
        console.print(f"[cyan]Conflict mode:[/cyan] {conflict}")
        if dry_run:
            console.print("[yellow]DRY RUN - no changes will be made[/yellow]")

        # Run import with progress
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Importing...", total=None)

            def update_progress(current: int, total: int, phase: str) -> None:
                progress.update(task, description=f"Importing {phase}... ({current}/{total})")

            service = TenantImportService(nx)
            result = service.import_tenant(options, update_progress)

        nx.close()

        # Show results
        console.print()
        table = Table(title="Import Complete" if result.success else "Import Failed")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green" if result.success else "red")

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
            console.print("[red]Errors:[/red]")
            for error in result.errors[:10]:  # Show first 10 errors
                console.print(f"  - {error.path}: {error.message}")
            if len(result.errors) > 10:
                console.print(f"  ... and {len(result.errors) - 10} more errors")
            sys.exit(1)

        # Show warnings if any
        if result.warnings:
            console.print()
            console.print("[yellow]Warnings:[/yellow]")
            for warning in result.warnings[:5]:
                console.print(f"  - {warning}")
            if len(result.warnings) > 5:
                console.print(f"  ... and {len(result.warnings) - 5} more warnings")

        if result.success:
            console.print()
            console.print("[green]✓ Import completed successfully[/green]")

    except Exception as e:
        handle_error(e)


@tenant.command(name="inspect")
@click.argument("bundle_path", type=click.Path(exists=True))
def inspect_bundle_cmd(bundle_path: str) -> None:
    """Inspect a .nexus bundle and show its contents.

    Examples:
        nexus tenant inspect /backup/acme.nexus
    """
    try:
        from nexus.portability import inspect_bundle

        info = inspect_bundle(bundle_path)

        table = Table(title=f"Bundle: {Path(bundle_path).name}")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Bundle ID", info["bundle_id"][:8] + "...")
        table.add_row("Format Version", info["format_version"])
        table.add_row("Nexus Version", info["nexus_version"])
        table.add_row("Source Tenant", info["source_tenant_id"])
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


@tenant.command(name="validate")
@click.argument("bundle_path", type=click.Path(exists=True))
def validate_bundle_cmd(bundle_path: str) -> None:
    """Validate a .nexus bundle integrity.

    Checks:
    - Manifest exists and is valid
    - Required files are present
    - Checksums match

    Examples:
        nexus tenant validate /backup/acme.nexus
    """
    try:
        from nexus.portability import validate_bundle

        console.print(f"[cyan]Validating:[/cyan] {bundle_path}")

        is_valid, errors = validate_bundle(bundle_path)

        if is_valid:
            console.print("[green]✓ Bundle is valid[/green]")
        else:
            console.print("[red]✗ Bundle validation failed:[/red]")
            for error in errors:
                console.print(f"  - {error}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)
