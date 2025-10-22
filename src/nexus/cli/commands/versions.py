"""Version Tracking Commands - Manage file version history.

CAS-backed version tracking for files and skills with full history.
Every file write creates a new version, preserving all previous versions.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register version tracking commands with the CLI.

    Args:
        cli: The main CLI group to register commands with
    """
    cli.add_command(version_group)


@click.group(name="versions")
def version_group() -> None:
    """Version Tracking - Manage file version history.

    CAS-backed version tracking for files and skills with full history.
    Every file write creates a new version, preserving all previous versions.

    Examples:
        nexus versions history /workspace/SKILL.md
        nexus versions diff /workspace/data.txt --v1 1 --v2 3
        nexus versions get /workspace/file.txt --version 2
        nexus versions rollback /workspace/file.txt --version 1
    """
    pass


@version_group.command(name="history")
@click.argument("path")
@click.option("--limit", type=int, default=None, help="Limit number of versions shown")
@add_backend_options
def version_history(path: str, limit: int | None, backend_config: BackendConfig) -> None:
    """Show version history for a file.

    Displays all versions of a file with metadata.

    Example:
        nexus version history /workspace/SKILL.md
        nexus version history /workspace/data.txt --limit 10
    """

    def format_size(size: int) -> str:
        """Format size in human-readable format."""
        size_float = float(size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_float < 1024.0:
                return f"{size_float:.1f} {unit}"
            size_float /= 1024.0
        return f"{size_float:.1f} PB"

    try:
        nx = get_filesystem(backend_config)
        versions = nx.list_versions(path)

        if not versions:
            console.print(f"[yellow]No version history found for: {path}[/yellow]")
            nx.close()
            return

        # Apply limit
        if limit:
            versions = versions[:limit]

        # Display table
        table = Table(title=f"Version History: {path}")
        table.add_column("Version", style="cyan", justify="right")
        table.add_column("Size", style="green")
        table.add_column("Created At")
        table.add_column("Created By")
        table.add_column("Source", style="yellow")
        table.add_column("Change Reason")

        for v in versions:
            created_at = v["created_at"].strftime("%Y-%m-%d %H:%M:%S") if v["created_at"] else "N/A"
            size = format_size(v["size"])
            table.add_row(
                str(v["version"]),
                size,
                created_at,
                v.get("created_by") or "-",
                v.get("source_type") or "original",
                v.get("change_reason") or "-",
            )

        console.print(table)
        console.print(f"\n[dim]Total versions: {len(versions)}[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@version_group.command(name="get")
@click.argument("path")
@click.option("--version", "-v", type=int, required=True, help="Version number to retrieve")
@click.option("--output", "-o", help="Output file path (default: stdout)")
@add_backend_options
def version_get(path: str, version: int, output: str | None, backend_config: BackendConfig) -> None:
    """Get a specific version of a file.

    Retrieves content from a specific version.

    Example:
        nexus version get /workspace/file.txt --version 2
        nexus version get /workspace/file.txt -v 1 -o old_version.txt
    """
    try:
        nx = get_filesystem(backend_config)
        content = nx.get_version(path, version)

        if output:
            # Write to file
            Path(output).write_bytes(content)
            console.print(f"[green]✓[/green] Wrote version {version} to: {output}")
        else:
            # Print to stdout
            try:
                console.print(content.decode("utf-8"))
            except UnicodeDecodeError:
                console.print("[yellow]Binary content (cannot display)[/yellow]")
                console.print("[dim]Use --output to save to file[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@version_group.command(name="diff")
@click.argument("path")
@click.option("--v1", type=int, required=True, help="First version number")
@click.option("--v2", type=int, required=True, help="Second version number")
@click.option(
    "--mode", type=click.Choice(["metadata", "content"]), default="content", help="Diff mode"
)
@add_backend_options
def version_diff(path: str, v1: int, v2: int, mode: str, backend_config: BackendConfig) -> None:
    """Compare two versions of a file.

    Shows differences between two versions.

    Example:
        nexus version diff /workspace/file.txt --v1 1 --v2 3
        nexus version diff /workspace/file.txt --v1 1 --v2 2 --mode metadata
    """

    def format_size(size: int) -> str:
        """Format size in human-readable format."""
        size_float = float(abs(size))
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_float < 1024.0:
                return f"{size_float:.1f} {unit}"
            size_float /= 1024.0
        return f"{size_float:.1f} PB"

    try:
        nx = get_filesystem(backend_config)
        diff = nx.diff_versions(path, v1, v2, mode=mode)

        if mode == "metadata":
            # diff is a dict in metadata mode
            if not isinstance(diff, dict):
                console.print("[red]Error: Expected metadata dict from diff[/red]")
                nx.close()
                return

            # Display metadata diff as table
            table = Table(title=f"Metadata Diff: v{v1} vs v{v2}")
            table.add_column("Property")
            table.add_column(f"Version {v1}", style="cyan")
            table.add_column(f"Version {v2}", style="green")

            table.add_row(
                "Size",
                format_size(cast(int, diff.get("size_v1", 0))),
                format_size(cast(int, diff.get("size_v2", 0))),
            )
            table.add_row(
                "Size Delta",
                "",
                f"{'+' if cast(int, diff.get('size_delta', 0)) > 0 else ''}{format_size(abs(cast(int, diff.get('size_delta', 0))))}",
            )
            table.add_row(
                "Content Hash",
                str(diff.get("content_hash_v1", ""))[:16] + "...",
                str(diff.get("content_hash_v2", ""))[:16] + "...",
            )
            table.add_row(
                "Content Changed",
                "",
                "[green]Yes[/green]" if diff.get("content_changed") else "[dim]No[/dim]",
            )

            created_at_v1 = diff.get("created_at_v1")
            created_at_v2 = diff.get("created_at_v2")
            table.add_row(
                "Created At",
                created_at_v1.strftime("%Y-%m-%d %H:%M:%S") if created_at_v1 else "N/A",
                created_at_v2.strftime("%Y-%m-%d %H:%M:%S") if created_at_v2 else "N/A",
            )

            console.print(table)
        else:
            # Display content diff (diff is a string in content mode)
            console.print(str(diff))

        nx.close()

    except Exception as e:
        handle_error(e)


@version_group.command(name="rollback")
@click.argument("path")
@click.option("--version", "-v", type=int, required=True, help="Version to rollback to")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@add_backend_options
def version_rollback(path: str, version: int, yes: bool, backend_config: BackendConfig) -> None:
    """Rollback file to a previous version.

    Reverts file content to an older version.

    Example:
        nexus version rollback /workspace/file.txt --version 2
        nexus version rollback /workspace/file.txt -v 1 --yes
    """
    try:
        nx = get_filesystem(backend_config)

        # Get current version for confirmation
        # Check if file exists
        if not nx.exists(path):
            console.print(f"[red]File not found: {path}[/red]")
            nx.close()
            return

        # Get version history to determine current version
        versions = nx.list_versions(path)
        if not versions:
            console.print(f"[yellow]No version history found for: {path}[/yellow]")
            nx.close()
            return

        current_version = versions[0]["version"]  # First entry is the latest

        if not yes:
            confirmed = click.confirm(f"Rollback {path} from v{current_version} to v{version}?")
            if not confirmed:
                console.print("Cancelled")
                nx.close()
                return

        # Perform rollback
        nx.rollback(path, version)

        console.print(f"[green]✓[/green] Rolled back {path} to version {version}")
        console.print(f"[dim]New version: {current_version + 1}[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)
