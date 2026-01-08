"""Nexus CLI Migration Commands - Version upgrades and system migrations.

Commands for upgrading between versions, importing data from external sources,
validating integrity, and managing rollbacks.

Issue #165: Migration Tools & Upgrade Paths
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    handle_error,
)

if TYPE_CHECKING:
    pass


@click.group(name="migrate")
def migrate() -> None:
    """Migration tools for version upgrades and data imports.

    Provides commands for:
    - Version upgrades and rollbacks
    - Bulk data imports from S3/GCS/local
    - Integrity validation
    - Migration history tracking

    Examples:
        nexus migrate status
        nexus migrate upgrade --from 0.5.0 --to 0.6.0 --backup
        nexus migrate validate --check-integrity
    """
    pass


@migrate.command(name="status")
@add_backend_options
def status(backend_config: BackendConfig) -> None:
    """Show migration status and history.

    Displays:
    - Current Nexus version
    - Migration history
    - Available backups

    Examples:
        nexus migrate status
    """
    try:
        import nexus as nexus_pkg
        from nexus.config import NexusConfig
        from nexus.migrations import VersionManager

        # Create config from backend_config
        config = NexusConfig(
            data_dir=backend_config.data_dir,
            db_path=backend_config.data_dir + "/nexus.db" if backend_config.data_dir else None,
        )

        manager = VersionManager(config)

        # Show current version
        console.print(f"[bold cyan]Nexus Version:[/bold cyan] {nexus_pkg.__version__}")
        console.print()

        # Show migration history
        console.print("[bold]Migration History:[/bold]")
        try:
            history = manager.get_migration_history()
            if history:
                table = Table()
                table.add_column("Time", style="dim")
                table.add_column("Type", style="cyan")
                table.add_column("From")
                table.add_column("To")
                table.add_column("Status")
                table.add_column("Duration")

                for entry in history[:10]:  # Show last 10
                    started = (
                        entry.started_at.strftime("%Y-%m-%d %H:%M") if entry.started_at else "N/A"
                    )
                    status_style = {
                        "completed": "green",
                        "failed": "red",
                        "running": "yellow",
                        "rolled_back": "magenta",
                    }.get(entry.status, "dim")

                    duration = ""
                    if entry.started_at and entry.completed_at:
                        delta = entry.completed_at - entry.started_at
                        duration = f"{delta.total_seconds():.1f}s"

                    table.add_row(
                        started,
                        entry.migration_type,
                        entry.from_version,
                        entry.to_version,
                        f"[{status_style}]{entry.status}[/{status_style}]",
                        duration,
                    )

                console.print(table)
            else:
                console.print("  [dim]No migration history found[/dim]")
        except Exception as e:
            console.print(f"  [dim]Could not load history: {e}[/dim]")

        console.print()

        # Show available backups
        console.print("[bold]Available Backups:[/bold]")
        backups = manager.list_backups()
        if backups:
            for backup in backups[:5]:  # Show last 5
                backup_time = backup.get("backup_time", "Unknown")
                version = backup.get("nexus_version", "Unknown")
                console.print(f"  - {backup_time} (v{version})")
                console.print(f"    [dim]{backup['path']}[/dim]")
        else:
            console.print("  [dim]No backups found[/dim]")

    except Exception as e:
        handle_error(e)


@migrate.command(name="plan")
@click.option("--from", "from_version", required=True, help="Source version")
@click.option("--to", "to_version", required=True, help="Target version")
@add_backend_options
def plan(from_version: str, to_version: str, backend_config: BackendConfig) -> None:
    """Show migration plan without executing (dry-run).

    Displays the steps that would be executed for a version upgrade.

    Examples:
        nexus migrate plan --from 0.5.0 --to 0.6.0
    """
    try:
        from nexus.config import NexusConfig
        from nexus.migrations import VersionManager

        config = NexusConfig(
            data_dir=backend_config.data_dir,
            db_path=backend_config.data_dir + "/nexus.db" if backend_config.data_dir else None,
        )

        manager = VersionManager(config)
        path = manager.plan_upgrade(from_version, to_version)

        if path is None:
            console.print(f"[red]No migration path found from {from_version} to {to_version}[/red]")
            sys.exit(1)

        console.print(f"[bold cyan]Migration Plan: {from_version} -> {to_version}[/bold cyan]")
        console.print()

        if not path.steps:
            console.print("[green]No migration steps needed (same version)[/green]")
            return

        console.print(f"[bold]Steps ({len(path.steps)}):[/bold]")
        for i, step in enumerate(path.steps, 1):
            flags = []
            if step.requires_backup:
                flags.append("[yellow]backup required[/yellow]")
            if step.is_destructive:
                flags.append("[red]destructive[/red]")

            flag_str = f" ({', '.join(flags)})" if flags else ""
            console.print(f"  {i}. [cyan]{step.name}[/cyan]{flag_str}")
            console.print(f"     {step.description}")
            console.print(f"     [dim]{step.from_version} -> {step.to_version}[/dim]")

        console.print()

        # Summary
        if path.total_requires_backup:
            console.print("[yellow]Backup recommended before migration[/yellow]")
        if path.has_destructive_steps:
            console.print("[red]Warning: Migration contains destructive steps[/red]")
        if not path.all_rollbackable:
            console.print("[yellow]Warning: Not all steps support rollback[/yellow]")

    except Exception as e:
        handle_error(e)


@migrate.command(name="upgrade")
@click.option("--from", "from_version", required=True, help="Source version")
@click.option("--to", "to_version", required=True, help="Target version")
@click.option("--backup/--no-backup", default=True, help="Create backup before migration")
@click.option("--dry-run", is_flag=True, help="Simulate without making changes")
@add_backend_options
def upgrade(
    from_version: str,
    to_version: str,
    backup: bool,
    dry_run: bool,
    backend_config: BackendConfig,
) -> None:
    """Upgrade from one version to another.

    Creates a backup before migration (unless --no-backup) and executes
    all required migration steps.

    Examples:
        nexus migrate upgrade --from 0.5.0 --to 0.6.0
        nexus migrate upgrade --from 0.5.0 --to 0.6.0 --no-backup
        nexus migrate upgrade --from 0.5.0 --to 0.6.0 --dry-run
    """
    try:
        from nexus.config import NexusConfig
        from nexus.migrations import VersionManager

        config = NexusConfig(
            data_dir=backend_config.data_dir,
            db_path=backend_config.data_dir + "/nexus.db" if backend_config.data_dir else None,
        )

        manager = VersionManager(config)

        if dry_run:
            console.print("[yellow]DRY RUN - No changes will be made[/yellow]")
            console.print()

        console.print(f"[bold]Upgrading: {from_version} -> {to_version}[/bold]")

        def progress_callback(message: str, current: int, total: int) -> None:
            console.print(f"  [{current}/{total}] {message}")

        result = manager.upgrade(
            from_version=from_version,
            to_version=to_version,
            backup=backup,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )

        console.print()

        if result.success:
            console.print("[bold green]Migration completed successfully![/bold green]")
        else:
            console.print("[bold red]Migration failed![/bold red]")

        # Show result details
        console.print(f"  Steps completed: {result.steps_completed}/{result.steps_total}")
        console.print(f"  Duration: {result.duration_seconds:.2f}s")

        if result.backup_path:
            console.print(f"  Backup: [dim]{result.backup_path}[/dim]")

        if result.warnings:
            console.print()
            console.print("[yellow]Warnings:[/yellow]")
            for warning in result.warnings:
                console.print(f"  - {warning}")

        if result.errors:
            console.print()
            console.print("[red]Errors:[/red]")
            for error in result.errors:
                console.print(f"  - {error}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@migrate.command(name="rollback")
@click.option("--to-version", required=True, help="Target version to rollback to")
@click.option("--from-backup", default=None, help="Restore from specific backup path")
@click.option("--dry-run", is_flag=True, help="Simulate without making changes")
@add_backend_options
def rollback(
    to_version: str,
    from_backup: str | None,
    dry_run: bool,
    backend_config: BackendConfig,
) -> None:
    """Rollback to a previous version.

    Can rollback using:
    1. Migration rollback functions (if available)
    2. Restore from a backup (--from-backup)

    Examples:
        nexus migrate rollback --to-version 0.5.0
        nexus migrate rollback --to-version 0.5.0 --from-backup /path/to/backup
        nexus migrate rollback --to-version 0.5.0 --dry-run
    """
    try:
        from nexus.config import NexusConfig
        from nexus.migrations import VersionManager

        config = NexusConfig(
            data_dir=backend_config.data_dir,
            db_path=backend_config.data_dir + "/nexus.db" if backend_config.data_dir else None,
        )

        manager = VersionManager(config)
        current_version = manager.get_current_version()

        if dry_run:
            console.print("[yellow]DRY RUN - No changes will be made[/yellow]")
            console.print()

        console.print(f"[bold]Rolling back: {current_version} -> {to_version}[/bold]")

        if from_backup:
            console.print(f"  Using backup: [dim]{from_backup}[/dim]")

        def progress_callback(message: str, current: int, total: int) -> None:
            console.print(f"  [{current}/{total}] {message}")

        result = manager.rollback(
            to_version=to_version,
            from_backup=from_backup,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )

        console.print()

        if result.success:
            console.print("[bold green]Rollback completed successfully![/bold green]")
        else:
            console.print("[bold red]Rollback failed![/bold red]")

        console.print(f"  Duration: {result.duration_seconds:.2f}s")

        if result.warnings:
            console.print()
            console.print("[yellow]Warnings:[/yellow]")
            for warning in result.warnings:
                console.print(f"  - {warning}")

        if result.errors:
            console.print()
            console.print("[red]Errors:[/red]")
            for error in result.errors:
                console.print(f"  - {error}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@migrate.command(name="backup")
@click.option("--list", "list_backups", is_flag=True, help="List available backups")
@add_backend_options
def backup_cmd(list_backups: bool, backend_config: BackendConfig) -> None:
    """Create or list backups.

    Examples:
        nexus migrate backup           # Create a new backup
        nexus migrate backup --list    # List existing backups
    """
    try:
        from nexus.config import NexusConfig
        from nexus.migrations import VersionManager

        config = NexusConfig(
            data_dir=backend_config.data_dir,
            db_path=backend_config.data_dir + "/nexus.db" if backend_config.data_dir else None,
        )

        manager = VersionManager(config)

        if list_backups:
            backups = manager.list_backups()
            if backups:
                console.print("[bold]Available Backups:[/bold]")
                table = Table()
                table.add_column("Time")
                table.add_column("Version")
                table.add_column("Path")

                for backup in backups:
                    table.add_row(
                        backup.get("backup_time", "Unknown"),
                        backup.get("nexus_version", "Unknown"),
                        backup["path"],
                    )

                console.print(table)
            else:
                console.print("[dim]No backups found[/dim]")
        else:
            console.print("[bold]Creating backup...[/bold]")
            backup_path = manager.create_backup()
            console.print(f"[green]Backup created:[/green] {backup_path}")

    except Exception as e:
        handle_error(e)


@migrate.command(name="restore")
@click.argument("backup_path", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Simulate without making changes")
@add_backend_options
def restore(backup_path: str, dry_run: bool, backend_config: BackendConfig) -> None:
    """Restore from a backup.

    Examples:
        nexus migrate restore /path/to/backup
        nexus migrate restore /path/to/backup --dry-run
    """
    try:
        from nexus.config import NexusConfig
        from nexus.migrations import VersionManager

        config = NexusConfig(
            data_dir=backend_config.data_dir,
            db_path=backend_config.data_dir + "/nexus.db" if backend_config.data_dir else None,
        )

        manager = VersionManager(config)

        if dry_run:
            console.print("[yellow]DRY RUN - Would restore from:[/yellow]")
            console.print(f"  {backup_path}")
            return

        console.print(f"[bold]Restoring from backup:[/bold] {backup_path}")

        if manager.restore_backup(backup_path):
            console.print("[green]Restore completed successfully![/green]")
        else:
            console.print("[red]Restore failed![/red]")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@migrate.command(name="import-s3")
@click.option("--bucket", required=True, help="S3 bucket name")
@click.option("--prefix", default="", help="Key prefix to import from")
@click.option("--target", required=True, help="Target path in Nexus")
@click.option("--overwrite", is_flag=True, help="Overwrite existing files")
@click.option("--dry-run", is_flag=True, help="Simulate without making changes")
@add_backend_options
def import_s3(
    bucket: str,
    prefix: str,
    target: str,
    overwrite: bool,
    dry_run: bool,
    backend_config: BackendConfig,
) -> None:
    """Import files from an S3 bucket.

    Requires AWS credentials (via environment or AWS CLI config).

    Examples:
        nexus migrate import-s3 --bucket my-bucket --prefix /data/ --target /workspace/
        nexus migrate import-s3 --bucket my-bucket --target /imports/ --dry-run
    """
    try:
        from nexus.cli.utils import get_filesystem
        from nexus.migrations.data_migrator import DataMigrator, ImportOptions

        if dry_run:
            console.print("[yellow]DRY RUN - No changes will be made[/yellow]")
            console.print()

        console.print(f"[bold]Importing from S3:[/bold] s3://{bucket}/{prefix}")
        console.print(f"[bold]Target:[/bold] {target}")
        console.print()

        nx = get_filesystem(backend_config)
        migrator = DataMigrator(nx)

        options = ImportOptions(
            source_type="s3",
            overwrite=overwrite,
            dry_run=dry_run,
        )

        def progress_callback(message: str, current: int, total: int) -> None:
            console.print(f"  [{current}/{total}] {message}")

        result = migrator.import_from_s3(
            bucket=bucket,
            prefix=prefix,
            target_path=target,
            options=options,
            progress_callback=progress_callback,
        )

        nx.close()

        console.print()
        _print_import_result(result, dry_run)

    except Exception as e:
        handle_error(e)


@migrate.command(name="import-gcs")
@click.option("--bucket", required=True, help="GCS bucket name")
@click.option("--prefix", default="", help="Blob prefix to import from")
@click.option("--target", required=True, help="Target path in Nexus")
@click.option("--overwrite", is_flag=True, help="Overwrite existing files")
@click.option("--dry-run", is_flag=True, help="Simulate without making changes")
@click.option("--credentials", default=None, help="Path to service account credentials JSON")
@add_backend_options
def import_gcs(
    bucket: str,
    prefix: str,
    target: str,
    overwrite: bool,
    dry_run: bool,
    credentials: str | None,
    backend_config: BackendConfig,
) -> None:
    """Import files from a Google Cloud Storage bucket.

    Requires GCS credentials (via GOOGLE_APPLICATION_CREDENTIALS or --credentials).

    Examples:
        nexus migrate import-gcs --bucket my-bucket --prefix /data/ --target /workspace/
        nexus migrate import-gcs --bucket my-bucket --target /imports/ --credentials creds.json
    """
    try:
        from nexus.cli.utils import get_filesystem
        from nexus.migrations.data_migrator import DataMigrator, ImportOptions

        if dry_run:
            console.print("[yellow]DRY RUN - No changes will be made[/yellow]")
            console.print()

        console.print(f"[bold]Importing from GCS:[/bold] gs://{bucket}/{prefix}")
        console.print(f"[bold]Target:[/bold] {target}")
        console.print()

        nx = get_filesystem(backend_config)
        migrator = DataMigrator(nx)

        options = ImportOptions(
            source_type="gcs",
            overwrite=overwrite,
            dry_run=dry_run,
        )

        def progress_callback(message: str, current: int, total: int) -> None:
            console.print(f"  [{current}/{total}] {message}")

        result = migrator.import_from_gcs(
            bucket=bucket,
            prefix=prefix,
            target_path=target,
            options=options,
            progress_callback=progress_callback,
            credentials_path=credentials,
        )

        nx.close()

        console.print()
        _print_import_result(result, dry_run)

    except Exception as e:
        handle_error(e)


@migrate.command(name="import-fs")
@click.option("--source", required=True, type=click.Path(exists=True), help="Source directory")
@click.option("--target", required=True, help="Target path in Nexus")
@click.option("--overwrite", is_flag=True, help="Overwrite existing files")
@click.option("--dry-run", is_flag=True, help="Simulate without making changes")
@add_backend_options
def import_fs(
    source: str,
    target: str,
    overwrite: bool,
    dry_run: bool,
    backend_config: BackendConfig,
) -> None:
    """Import files from local filesystem.

    Examples:
        nexus migrate import-fs --source /local/data --target /workspace/
        nexus migrate import-fs --source ./docs --target /docs/ --dry-run
    """
    try:
        from nexus.cli.utils import get_filesystem
        from nexus.migrations.data_migrator import DataMigrator, ImportOptions

        if dry_run:
            console.print("[yellow]DRY RUN - No changes will be made[/yellow]")
            console.print()

        console.print(f"[bold]Importing from:[/bold] {source}")
        console.print(f"[bold]Target:[/bold] {target}")
        console.print()

        nx = get_filesystem(backend_config)
        migrator = DataMigrator(nx)

        options = ImportOptions(
            source_type="local",
            overwrite=overwrite,
            dry_run=dry_run,
        )

        def progress_callback(message: str, current: int, total: int) -> None:
            console.print(f"  [{current}/{total}] {message}")

        result = migrator.import_from_local(
            source_path=source,
            target_path=target,
            options=options,
            progress_callback=progress_callback,
        )

        nx.close()

        console.print()
        _print_import_result(result, dry_run)

    except Exception as e:
        handle_error(e)


@migrate.command(name="validate")
@click.option("--check-integrity", is_flag=True, help="Run full integrity checks")
@click.option("--sample-size", default=100, help="Number of files to sample for content validation")
@add_backend_options
def validate(
    check_integrity: bool,
    sample_size: int,  # noqa: ARG001 - Reserved for future use
    backend_config: BackendConfig,
) -> None:
    """Validate data integrity.

    Runs validation checks on metadata and content integrity.

    Examples:
        nexus migrate validate
        nexus migrate validate --check-integrity
        nexus migrate validate --check-integrity --sample-size 500
    """
    try:
        from nexus.cli.utils import get_filesystem
        from nexus.migrations.validators import IntegrityValidator

        console.print("[bold]Running validation checks...[/bold]")
        console.print()

        nx = get_filesystem(backend_config)
        validator = IntegrityValidator(nx)

        def progress_callback(message: str, current: int, total: int) -> None:
            console.print(f"  [{current}/{total}] {message}")

        if check_integrity:
            result = validator.full_validation(progress_callback=progress_callback)
        else:
            result = validator.validate_metadata_integrity()

        nx.close()

        console.print()

        if result.valid:
            console.print("[bold green]Validation PASSED[/bold green]")
        else:
            console.print("[bold red]Validation FAILED[/bold red]")

        console.print(f"  Files checked: {result.checked_files}")
        console.print(f"  Corrupted: {result.corrupted_files}")
        console.print(f"  Missing content: {result.missing_content}")
        console.print(f"  Orphaned content: {result.orphaned_content}")

        if result.warnings:
            console.print()
            console.print("[yellow]Warnings:[/yellow]")
            for warning in result.warnings[:10]:
                console.print(f"  - {warning}")
            if len(result.warnings) > 10:
                console.print(f"  ... and {len(result.warnings) - 10} more")

        if result.errors:
            console.print()
            console.print("[red]Errors:[/red]")
            for error in result.errors[:10]:
                console.print(f"  - {error}")
            if len(result.errors) > 10:
                console.print(f"  ... and {len(result.errors) - 10} more")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


def _print_import_result(result, dry_run: bool) -> None:
    """Print import result summary.

    Args:
        result: ImportResult to print
        dry_run: Whether this was a dry run
    """
    if dry_run:
        console.print("[bold yellow]DRY RUN RESULTS:[/bold yellow]")
    else:
        if result.errors:
            console.print("[bold red]Import completed with errors[/bold red]")
        else:
            console.print("[bold green]Import completed successfully![/bold green]")

    console.print(f"  Files imported: [green]{result.files_imported}[/green]")
    console.print(f"  Files skipped: [yellow]{result.files_skipped}[/yellow]")
    console.print(f"  Files failed: [red]{result.files_failed}[/red]")
    console.print(f"  Bytes transferred: {result.bytes_transferred:,}")
    console.print(f"  Duration: {result.duration_seconds:.2f}s")

    if result.errors:
        console.print()
        console.print("[red]Errors:[/red]")
        for error in result.errors[:5]:
            console.print(f"  - {error}")
        if len(result.errors) > 5:
            console.print(f"  ... and {len(result.errors) - 5} more")


def register_commands(cli: click.Group) -> None:
    """Register all migrate commands to the CLI group.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(migrate)
