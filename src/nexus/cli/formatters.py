"""CLI output formatters - Rich formatting utilities for Nexus CLI."""

from __future__ import annotations

from datetime import datetime

from rich.table import Table

from nexus.cli.utils import console


def format_permissions(mode: int | None) -> str:
    """Format file mode as permission string.

    Args:
        mode: File mode (e.g., 0o644)

    Returns:
        Permission string (e.g., "rw-r--r--")
    """
    if mode is None:
        return "---------"

    # Convert mode to rwx format
    perms = []
    for shift in [6, 3, 0]:  # owner, group, other
        perm = (mode >> shift) & 0o7
        perms.append("r" if perm & 0o4 else "-")
        perms.append("w" if perm & 0o2 else "-")
        perms.append("x" if perm & 0o1 else "-")
    return "".join(perms)


def format_size(size: int) -> str:
    """Format file size in human-readable format.

    Args:
        size: Size in bytes

    Returns:
        Formatted size string (e.g., "1.5 MB")
    """
    size_float = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_float < 1024.0:
            return f"{size_float:.1f} {unit}"
        size_float /= 1024.0
    return f"{size_float:.1f} PB"


def format_timestamp(dt: datetime | None) -> str:
    """Format datetime as string.

    Args:
        dt: Datetime object

    Returns:
        Formatted timestamp string
    """
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def create_file_table(title: str, show_permissions: bool = True) -> Table:
    """Create a Rich table for file listings.

    Args:
        title: Table title
        show_permissions: Whether to include permission columns

    Returns:
        Rich Table object
    """
    table = Table(title=title)

    if show_permissions:
        table.add_column("Permissions", style="magenta")
        table.add_column("Owner", style="blue")
        table.add_column("Group", style="blue")

    table.add_column("Path", style="cyan")
    table.add_column("Size", justify="right", style="green")
    table.add_column("Modified", style="yellow")

    return table


def add_file_row(
    table: Table,
    path: str,
    size: int,
    modified_at: datetime | None,
    permissions: str | None = None,
    owner: str | None = None,
    group: str | None = None,
) -> None:
    """Add a file row to the table.

    Args:
        table: Rich Table object
        path: File path
        size: File size in bytes
        modified_at: Last modified timestamp
        permissions: Permission string (optional)
        owner: Owner name (optional)
        group: Group name (optional)
    """
    size_str = f"{size:,} bytes"
    modified_str = format_timestamp(modified_at)

    if permissions is not None:
        owner_str = owner if owner else "-"
        group_str = group if group else "-"
        table.add_row(permissions, owner_str, group_str, path, size_str, modified_str)
    else:
        table.add_row(path, size_str, modified_str)


def print_success(message: str) -> None:
    """Print success message."""
    console.print(f"[green]✓[/green] {message}")


def print_error(message: str) -> None:
    """Print error message."""
    console.print(f"[red]✗[/red] {message}")


def print_warning(message: str) -> None:
    """Print warning message."""
    console.print(f"[yellow]⚠[/yellow] {message}")


def print_info(message: str) -> None:
    """Print info message."""
    console.print(f"[cyan]ℹ[/cyan] {message}")
