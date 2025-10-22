"""Directory operation commands - ls, mkdir, rmdir, tree."""

from __future__ import annotations

from typing import Any, cast

import click
from rich.table import Table

from nexus.cli.formatters import format_permissions, format_timestamp
from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)
from nexus.core.nexus_fs import NexusFS


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
@add_backend_options
def list_files(
    path: str,
    recursive: bool,
    long: bool,
    backend_config: BackendConfig,
) -> None:
    """List files in a directory.

    Examples:
        nexus ls /workspace
        nexus ls /workspace --recursive
        nexus ls /workspace -l
        nexus ls /workspace --backend=gcs --gcs-bucket=my-bucket
    """
    try:
        nx = get_filesystem(backend_config)

        if long:
            # Detailed listing
            files_raw = nx.list(path, recursive=recursive, details=True)
            files = cast(list[dict[str, Any]], files_raw)

            if not files:
                console.print(f"[yellow]No files found in {path}[/yellow]")
                nx.close()
                return

            table = Table(title=f"Files in {path}")
            table.add_column("Permissions", style="magenta")
            table.add_column("Owner", style="blue")
            table.add_column("Group", style="blue")
            table.add_column("Path", style="cyan")
            table.add_column("Size", justify="right", style="green")
            table.add_column("Modified", style="yellow")

            # Get metadata with permissions
            if isinstance(nx, NexusFS):
                for file in files:
                    meta = nx.metadata.get(file["path"])

                    # Format permissions
                    perms_str = format_permissions(meta.mode if meta else None)
                    owner_str = meta.owner if meta and meta.owner else "-"
                    group_str = meta.group if meta and meta.group else "-"
                    size_str = f"{file['size']:,} bytes"
                    modified_str = format_timestamp(file.get("modified_at"))

                    table.add_row(
                        perms_str, owner_str, group_str, file["path"], size_str, modified_str
                    )
            else:
                # Remote FS - no permission support yet
                for file in files:
                    size_str = f"{file['size']:,} bytes"
                    modified_str = format_timestamp(file.get("modified_at"))
                    table.add_row("---------", "-", "-", file["path"], size_str, modified_str)

            console.print(table)
        else:
            # Simple listing
            files_raw = nx.list(path, recursive=recursive)
            file_paths = cast(list[str], files_raw)

            if not file_paths:
                console.print(f"[yellow]No files found in {path}[/yellow]")
                nx.close()
                return

            for file_path in file_paths:
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
