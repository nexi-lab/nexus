"""Directory operation commands - ls, mkdir, rmdir, tree."""

from __future__ import annotations

from typing import Any, cast

import click
from rich.table import Table

from nexus.cli.formatters import format_timestamp
from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register all directory operation commands."""
    cli.add_command(list_files)
    cli.add_command(mkdir)
    cli.add_command(rmdir)
    cli.add_command(tree)


@click.command(name="ls")
@click.argument("path", default="/", type=str)
@click.option("-r", "--recursive", is_flag=True, help="List files recursively")
@click.option("-l", "--long", is_flag=True, help="Show detailed information")
@click.option(
    "--at-operation",
    type=str,
    help="List files at a historical operation point (time-travel debugging)",
)
@add_backend_options
def list_files(
    path: str,
    recursive: bool,
    long: bool,
    at_operation: str | None,
    backend_config: BackendConfig,
) -> None:
    """List files in a directory.

    Examples:
        nexus ls /workspace
        nexus ls /workspace --recursive
        nexus ls /workspace -l
        nexus ls /workspace --backend=gcs --gcs-bucket=my-bucket

        # Time-travel: List files at historical operation point
        nexus ls /workspace --at-operation op_abc123
    """
    # Import at function level - needed for both time-travel and regular long listing
    from nexus.core.nexus_fs import NexusFS

    try:
        nx = get_filesystem(backend_config)

        if at_operation:
            # Time-travel: List files at historical operation point
            try:
                from nexus.storage.time_travel import TimeTravelReader
            except ImportError as e:
                console.print(f"[red]Error:[/red] Failed to import time-travel modules: {e}")
                nx.close()
                return

            if not isinstance(nx, NexusFS):
                console.print("[red]Error:[/red] Time-travel is only supported with local NexusFS")
                nx.close()
                return

            # Create time-travel reader with a session
            with nx.metadata.SessionLocal() as session:
                time_travel = TimeTravelReader(session, nx.backend)

                # Get directory listing at operation
                files = time_travel.list_files_at_operation(
                    path, at_operation, tenant_id=nx.tenant_id, recursive=recursive
                )

            nx.close()

            if not files:
                console.print(
                    f"[yellow]No files found in {path} at operation {at_operation}[/yellow]"
                )
                return

            # Display time-travel info
            console.print(
                f"[bold cyan]Time-Travel Mode - Files at operation {at_operation}[/bold cyan]"
            )
            console.print()

            if long:
                # Detailed listing
                table = Table(title=f"Files in {path}")
                table.add_column("Type", style="magenta", width=4)
                table.add_column("Path", style="cyan")
                table.add_column("Size", justify="right", style="green")
                table.add_column("Owner", style="blue")
                table.add_column("Group", style="blue")
                table.add_column("Mode", style="magenta")
                table.add_column("Modified", style="yellow")

                for file in files:
                    # Check if directory from time-travel data
                    is_dir = nx.is_directory(file["path"])
                    type_str = "dir" if is_dir else "file"
                    path_display = f"{file['path']}/" if is_dir else file["path"]
                    size_str = f"{file['size']:,} bytes" if not is_dir else "-"
                    owner_str = file.get("owner") or "-"
                    group_str = file.get("group") or "-"
                    mode_str = str(file.get("mode") or "-")
                    modified_str = file.get("modified_at") or "-"

                    table.add_row(
                        type_str,
                        path_display,
                        size_str,
                        owner_str,
                        group_str,
                        mode_str,
                        modified_str,
                    )

                console.print(table)
            else:
                # Simple listing
                for file in files:
                    is_dir = nx.is_directory(file["path"])
                    if is_dir:
                        console.print(f"  [bold cyan]{file['path']}/[/bold cyan]")
                    else:
                        console.print(f"  {file['path']}")

            return

        if long:
            # Detailed listing
            files_raw = nx.list(path, recursive=recursive, details=True)
            files = cast(list[dict[str, Any]], files_raw)

            if not files:
                console.print(f"[yellow]No files found in {path}[/yellow]")
                nx.close()
                return

            table = Table(title=f"Files in {path}")
            table.add_column("Type", style="magenta", width=4)
            table.add_column("Permissions", style="magenta")
            table.add_column("Owner", style="blue")
            table.add_column("Group", style="blue")
            table.add_column("Path", style="cyan")
            table.add_column("Size", justify="right", style="green")
            table.add_column("Modified", style="yellow")

            # Get metadata with permissions
            if isinstance(nx, NexusFS):
                for file in files:
                    # Use is_directory from metadata if available, otherwise check via API
                    is_dir = file.get("is_directory", False)
                    type_str = "dir" if is_dir else "file"
                    # Format permissions (UNIX permissions removed - using ReBAC)
                    perms_str = "---------"  # Placeholder since UNIX permissions are deprecated
                    owner_str = "-"  # Owner managed through ReBAC
                    group_str = "-"  # Group managed through ReBAC
                    size_str = f"{file['size']:,} bytes" if not is_dir else "-"
                    modified_str = format_timestamp(file.get("modified_at"))

                    # Format path with directory indicator
                    path_display = f"{file['path']}/" if is_dir else file["path"]

                    table.add_row(
                        type_str,
                        perms_str,
                        owner_str,
                        group_str,
                        path_display,
                        size_str,
                        modified_str,
                    )
            else:
                # Remote FS - no permission support yet
                for file in files:
                    # Use is_directory from metadata if available, otherwise check via API
                    is_dir = file.get("is_directory", False)
                    type_str = "dir" if is_dir else "file"
                    size_str = f"{file['size']:,} bytes" if not is_dir else "-"
                    modified_str = format_timestamp(file.get("modified_at"))
                    path_display = f"{file['path']}/" if is_dir else file["path"]
                    table.add_row(
                        type_str, "---------", "-", "-", path_display, size_str, modified_str
                    )

            console.print(table)
        else:
            # Simple listing - use details to get is_directory information
            files_raw = nx.list(path, recursive=recursive, details=True)
            files = cast(list[dict[str, Any]], files_raw)

            if not files:
                console.print(f"[yellow]No files found in {path}[/yellow]")
                nx.close()
                return

            for file in files:
                # Use is_directory from metadata
                is_dir = file.get("is_directory", False)
                file_path = file["path"]
                if is_dir:
                    console.print(f"  [bold cyan]{file_path}/[/bold cyan]")
                else:
                    console.print(f"  {file_path}")

        nx.close()
    except Exception as e:
        handle_error(e)


@click.command()
@click.argument("path", type=str)
@click.option("-p", "--parents", is_flag=True, help="Create parent directories as needed")
@add_backend_options
def mkdir(
    path: str,
    parents: bool,
    backend_config: BackendConfig,
) -> None:
    """Create a directory.

    Examples:
        nexus mkdir /workspace/data
        nexus mkdir /workspace/deep/nested/dir --parents
    """
    try:
        nx = get_filesystem(backend_config)
        nx.mkdir(path, parents=parents, exist_ok=True)
        nx.close()

        console.print(f"[green]✓[/green] Created directory [cyan]{path}[/cyan]")
    except Exception as e:
        handle_error(e)


@click.command()
@click.argument("path", type=str)
@click.option("-r", "--recursive", is_flag=True, help="Remove directory and contents")
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@add_backend_options
def rmdir(
    path: str,
    recursive: bool,
    force: bool,
    backend_config: BackendConfig,
) -> None:
    """Remove a directory.

    Examples:
        nexus rmdir /workspace/data
        nexus rmdir /workspace/data --recursive --force
    """
    try:
        nx = get_filesystem(backend_config)

        # Confirm deletion unless --force
        if not force and not click.confirm(f"Remove directory {path}?"):
            console.print("[yellow]Cancelled[/yellow]")
            nx.close()
            return

        nx.rmdir(path, recursive=recursive)
        nx.close()

        console.print(f"[green]✓[/green] Removed directory [cyan]{path}[/cyan]")
    except Exception as e:
        handle_error(e)


@click.command(name="tree")
@click.argument("path", default="/", type=str)
@click.option("-L", "--level", type=int, default=None, help="Max depth to display")
@click.option("--show-size", is_flag=True, help="Show file sizes")
@add_backend_options
def tree(
    path: str,
    level: int | None,
    show_size: bool,
    backend_config: BackendConfig,
) -> None:
    """Display directory tree structure.

    Shows an ASCII tree view of files and directories with optional
    size information and depth limiting.

    Examples:
        nexus tree /workspace
        nexus tree /workspace -L 2
        nexus tree /workspace --show-size
    """
    try:
        nx = get_filesystem(backend_config)

        # Get all files recursively
        files_raw = nx.list(path, recursive=True, details=show_size)
        nx.close()

        if not files_raw:
            console.print(f"[yellow]No files found in {path}[/yellow]")
            return

        # Build tree structure
        from collections import defaultdict
        from pathlib import PurePosixPath

        tree_dict: dict[str, Any] = defaultdict(dict)

        if show_size:
            files = cast(list[dict[str, Any]], files_raw)
            for file in files:
                file_path = file["path"]
                parts = PurePosixPath(file_path).parts
                current = tree_dict
                for i, part in enumerate(parts):
                    if i == len(parts) - 1:  # Leaf node (file)
                        current[part] = file["size"]
                    else:  # Directory
                        if part not in current or not isinstance(current[part], dict):
                            current[part] = {}
                        current = current[part]
        else:
            file_paths = cast(list[str], files_raw)
            for file_path in file_paths:
                parts = PurePosixPath(file_path).parts
                current = tree_dict
                for i, part in enumerate(parts):
                    if i == len(parts) - 1:  # Leaf node (file)
                        current[part] = None
                    else:  # Directory
                        if part not in current or not isinstance(current[part], dict):
                            current[part] = {}
                        current = current[part]

        # Display tree
        def format_size(size: int) -> str:
            """Format size in human-readable format."""
            size_float = float(size)
            for unit in ["B", "KB", "MB", "GB", "TB"]:
                if size_float < 1024.0:
                    return f"{size_float:.1f} {unit}"
                size_float /= 1024.0
            return f"{size_float:.1f} PB"

        def print_tree(
            node: dict[str, Any],
            prefix: str = "",
            current_level: int = 0,
        ) -> tuple[int, int]:
            """Recursively print tree structure. Returns (file_count, total_size)."""
            if level is not None and current_level >= level:
                return 0, 0

            items = sorted(node.items())
            total_files = 0
            total_size = 0

            for i, (name, value) in enumerate(items):
                is_last_item = i == len(items) - 1
                connector = "└── " if is_last_item else "├── "
                extension = "    " if is_last_item else "│   "

                if isinstance(value, dict):
                    # Directory
                    console.print(f"{prefix}{connector}[bold cyan]{name}/[/bold cyan]")
                    files, size = print_tree(
                        value,
                        prefix + extension,
                        current_level + 1,
                    )
                    total_files += files
                    total_size += size
                else:
                    # File
                    total_files += 1
                    if show_size and value is not None:
                        size_str = format_size(value)
                        console.print(f"{prefix}{connector}{name} [dim]({size_str})[/dim]")
                        total_size += value
                    else:
                        console.print(f"{prefix}{connector}{name}")

            return total_files, total_size

        # Print header
        console.print(f"[bold green]{path}[/bold green]")

        # Print tree
        file_count, total_size = print_tree(tree_dict)

        # Print summary
        console.print()
        if show_size:
            console.print(f"[dim]{file_count} files, {format_size(total_size)} total[/dim]")
        else:
            console.print(f"[dim]{file_count} files[/dim]")

    except Exception as e:
        handle_error(e)
