"""Nexus CLI - Command-line interface for Nexus filesystem operations.

Beautiful CLI with Click and Rich for file operations, discovery, and management.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import click
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

import nexus
from nexus import NexusFilesystem
from nexus.core.embedded import Embedded
from nexus.core.exceptions import NexusError, NexusFileNotFoundError

console = Console()

# Global options
DATA_DIR_OPTION = click.option(
    "--data-dir",
    type=click.Path(),
    default="./nexus-data",
    help="Path to Nexus data directory",
    show_default=True,
)

CONFIG_OPTION = click.option(
    "--config",
    type=click.Path(exists=True),
    default=None,
    help="Path to Nexus config file (nexus.yaml)",
)


def get_filesystem(data_dir: str, config_path: str | None = None) -> NexusFilesystem:
    """Get Nexus filesystem instance."""
    try:
        if config_path:
            # Use explicit config file
            return nexus.connect(config=config_path)
        else:
            # Use data_dir or auto-discover
            return nexus.connect(config={"data_dir": data_dir})
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(1)


def handle_error(e: Exception) -> None:
    """Handle errors with beautiful output."""
    if isinstance(e, NexusFileNotFoundError):
        console.print(f"[red]Error:[/red] File not found: {e}")
    elif isinstance(e, NexusError):
        console.print(f"[red]Nexus Error:[/red] {e}")
    else:
        console.print(f"[red]Unexpected error:[/red] {e}")
    sys.exit(1)


@click.group()
@click.version_option(version=nexus.__version__, prog_name="nexus")
def main() -> None:
    """
    Nexus - AI-Native Distributed Filesystem

    Beautiful command-line interface for file operations, discovery, and management.
    """
    pass


@main.command()
@click.argument("path", default="./nexus-workspace", type=click.Path())
def init(path: str) -> None:
    """Initialize a new Nexus workspace.

    Creates a new Nexus workspace with the following structure:
    - nexus-data/    # Metadata and content storage
    - workspace/     # Agent-specific scratch space
    - shared/        # Shared data between agents

    Example:
        nexus init ./my-workspace
    """
    workspace_path = Path(path)
    data_dir = workspace_path / "nexus-data"

    try:
        # Create workspace structure
        workspace_path.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        # Initialize Nexus
        nx = nexus.connect(config={"data_dir": str(data_dir)})

        # Create default directories
        nx.mkdir("/workspace", exist_ok=True)
        nx.mkdir("/shared", exist_ok=True)

        nx.close()

        console.print(
            f"[green]✓[/green] Initialized Nexus workspace at [cyan]{workspace_path}[/cyan]"
        )
        console.print(f"  Data directory: [cyan]{data_dir}[/cyan]")
        console.print(f"  Workspace: [cyan]{workspace_path / 'workspace'}[/cyan]")
        console.print(f"  Shared: [cyan]{workspace_path / 'shared'}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command(name="ls")
@click.argument("path", default="/", type=str)
@click.option("-r", "--recursive", is_flag=True, help="List files recursively")
@click.option("-l", "--long", is_flag=True, help="Show detailed information")
@CONFIG_OPTION
@DATA_DIR_OPTION
def list_files(path: str, recursive: bool, long: bool, config: str | None, data_dir: str) -> None:
    """List files in a directory.

    Examples:
        nexus ls /workspace
        nexus ls /workspace --recursive
        nexus ls /workspace -l
    """
    try:
        nx = get_filesystem(data_dir, config)

        if long:
            # Detailed listing
            files_raw = nx.list(path, recursive=recursive, details=True)
            files = cast(list[dict[str, Any]], files_raw)

            if not files:
                console.print(f"[yellow]No files found in {path}[/yellow]")
                nx.close()
                return

            table = Table(title=f"Files in {path}")
            table.add_column("Path", style="cyan")
            table.add_column("Size", justify="right", style="green")
            table.add_column("Modified", style="yellow")
            table.add_column("ETag", style="dim")

            for file in files:
                size_str = f"{file['size']:,} bytes"
                modified_str = file["modified_at"].strftime("%Y-%m-%d %H:%M:%S")
                etag_str = file["etag"][:12] + "..." if file["etag"] else "N/A"
                table.add_row(file["path"], size_str, modified_str, etag_str)

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


@main.command()
@click.argument("path", type=str)
@CONFIG_OPTION
@DATA_DIR_OPTION
def cat(path: str, config: str | None, data_dir: str) -> None:
    """Display file contents.

    Examples:
        nexus cat /workspace/data.txt
        nexus cat /workspace/code.py
    """
    try:
        nx = get_filesystem(data_dir, config)
        content = nx.read(path)
        nx.close()

        # Try to detect file type for syntax highlighting
        try:
            text = content.decode("utf-8")

            # Simple syntax highlighting based on extension
            if path.endswith(".py"):
                syntax = Syntax(text, "python", theme="monokai", line_numbers=True)
                console.print(syntax)
            elif path.endswith(".json"):
                syntax = Syntax(text, "json", theme="monokai", line_numbers=True)
                console.print(syntax)
            elif path.endswith((".md", ".markdown")):
                syntax = Syntax(text, "markdown", theme="monokai")
                console.print(syntax)
            else:
                console.print(text)
        except UnicodeDecodeError:
            console.print(f"[yellow]Binary file ({len(content)} bytes)[/yellow]")
            console.print(f"[dim]{content[:100]!r}...[/dim]")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("path", type=str)
@click.argument("content", type=str, required=False)
@click.option("-i", "--input", "input_file", type=click.File("rb"), help="Read from file or stdin")
@CONFIG_OPTION
@DATA_DIR_OPTION
def write(
    path: str,
    content: str | None,
    input_file: Any,
    config: str | None,
    data_dir: str,
) -> None:
    """Write content to a file.

    Examples:
        nexus write /workspace/data.txt "Hello World"
        echo "Hello World" | nexus write /workspace/data.txt --input -
        nexus write /workspace/data.txt --input local_file.txt
    """
    try:
        nx = get_filesystem(data_dir, config)

        # Determine content source
        if input_file:
            file_content = input_file.read()
        elif content == "-":
            # Read from stdin
            file_content = sys.stdin.buffer.read()
        elif content:
            file_content = content.encode("utf-8")
        else:
            console.print("[red]Error:[/red] Must provide content or use --input")
            sys.exit(1)

        nx.write(path, file_content)
        nx.close()

        console.print(f"[green]✓[/green] Wrote {len(file_content)} bytes to [cyan]{path}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("source", type=str)
@click.argument("dest", type=str)
@CONFIG_OPTION
@DATA_DIR_OPTION
def cp(source: str, dest: str, config: str | None, data_dir: str) -> None:
    """Copy a file.

    Examples:
        nexus cp /workspace/source.txt /workspace/dest.txt
    """
    try:
        nx = get_filesystem(data_dir, config)

        # Read source
        content = nx.read(source)

        # Write to destination
        nx.write(dest, content)

        nx.close()

        console.print(f"[green]✓[/green] Copied [cyan]{source}[/cyan] → [cyan]{dest}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("path", type=str)
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@CONFIG_OPTION
@DATA_DIR_OPTION
def rm(path: str, force: bool, config: str | None, data_dir: str) -> None:
    """Delete a file.

    Examples:
        nexus rm /workspace/data.txt
        nexus rm /workspace/data.txt --force
    """
    try:
        nx = get_filesystem(data_dir, config)

        # Check if file exists
        if not nx.exists(path):
            console.print(f"[yellow]File does not exist:[/yellow] {path}")
            nx.close()
            return

        # Confirm deletion unless --force
        if not force and not click.confirm(f"Delete {path}?"):
            console.print("[yellow]Cancelled[/yellow]")
            nx.close()
            return

        nx.delete(path)
        nx.close()

        console.print(f"[green]✓[/green] Deleted [cyan]{path}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("pattern", type=str)
@click.option("-p", "--path", default="/", help="Base path to search from")
@CONFIG_OPTION
@DATA_DIR_OPTION
def glob(pattern: str, path: str, config: str | None, data_dir: str) -> None:
    """Find files matching a glob pattern.

    Supports:
    - * (matches any characters except /)
    - ** (matches any characters including /)
    - ? (matches single character)
    - [...] (character classes)

    Examples:
        nexus glob "**/*.py"
        nexus glob "*.txt" --path /workspace
        nexus glob "test_*.py"
    """
    try:
        nx = get_filesystem(data_dir, config)
        matches = nx.glob(pattern, path)
        nx.close()

        if not matches:
            console.print(f"[yellow]No files match pattern:[/yellow] {pattern}")
            return

        console.print(f"[green]Found {len(matches)} files matching[/green] [cyan]{pattern}[/cyan]:")
        for match in matches:
            console.print(f"  {match}")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("pattern", type=str)
@click.option("-p", "--path", default="/", help="Base path to search from")
@click.option("-f", "--file-pattern", help="Filter files by glob pattern (e.g., *.py)")
@click.option("-i", "--ignore-case", is_flag=True, help="Case-insensitive search")
@click.option("-n", "--max-results", default=100, help="Maximum results to show")
@CONFIG_OPTION
@DATA_DIR_OPTION
def grep(
    pattern: str,
    path: str,
    file_pattern: str | None,
    ignore_case: bool,
    max_results: int,
    config: str | None,
    data_dir: str,
) -> None:
    """Search file contents using regex patterns.

    Examples:
        nexus grep "TODO"
        nexus grep "def \\w+" --file-pattern "**/*.py"
        nexus grep "error" --ignore-case
        nexus grep "TODO" --path /workspace
    """
    try:
        nx = get_filesystem(data_dir, config)
        matches = nx.grep(
            pattern,
            path=path,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
        )
        nx.close()

        if not matches:
            console.print(f"[yellow]No matches found for:[/yellow] {pattern}")
            return

        console.print(f"[green]Found {len(matches)} matches[/green] for [cyan]{pattern}[/cyan]:\n")

        current_file = None
        for match in matches:
            if match["file"] != current_file:
                current_file = match["file"]
                console.print(f"[bold cyan]{current_file}[/bold cyan]")

            console.print(f"  [yellow]{match['line']}:[/yellow] {match['content']}")
            console.print(f"      [dim]Match: [green]{match['match']}[/green][/dim]")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("path", type=str)
@click.option("-p", "--parents", is_flag=True, help="Create parent directories as needed")
@CONFIG_OPTION
@DATA_DIR_OPTION
def mkdir(path: str, parents: bool, config: str | None, data_dir: str) -> None:
    """Create a directory.

    Examples:
        nexus mkdir /workspace/data
        nexus mkdir /workspace/deep/nested/dir --parents
    """
    try:
        nx = get_filesystem(data_dir, config)
        nx.mkdir(path, parents=parents, exist_ok=True)
        nx.close()

        console.print(f"[green]✓[/green] Created directory [cyan]{path}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("path", type=str)
@click.option("-r", "--recursive", is_flag=True, help="Remove directory and contents")
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@CONFIG_OPTION
@DATA_DIR_OPTION
def rmdir(path: str, recursive: bool, force: bool, config: str | None, data_dir: str) -> None:
    """Remove a directory.

    Examples:
        nexus rmdir /workspace/data
        nexus rmdir /workspace/data --recursive --force
    """
    try:
        nx = get_filesystem(data_dir, config)

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


@main.command()
@click.argument("path", type=str)
@CONFIG_OPTION
@DATA_DIR_OPTION
def info(path: str, config: str | None, data_dir: str) -> None:
    """Show detailed file information.

    Examples:
        nexus info /workspace/data.txt
    """
    try:
        nx = get_filesystem(data_dir, config)

        # Check if file exists first
        if not nx.exists(path):
            console.print(f"[yellow]File not found:[/yellow] {path}")
            nx.close()
            return

        # Get file metadata from metadata store
        # Note: Only Embedded mode has direct metadata access
        if not isinstance(nx, Embedded):
            console.print("[red]Error:[/red] File info is only available in embedded mode")
            nx.close()
            return

        file_meta = nx.metadata.get(path)
        nx.close()

        if not file_meta:
            console.print(f"[yellow]File not found:[/yellow] {path}")
            return

        table = Table(title=f"File Information: {path}")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        created_str = (
            file_meta.created_at.strftime("%Y-%m-%d %H:%M:%S") if file_meta.created_at else "N/A"
        )
        modified_str = (
            file_meta.modified_at.strftime("%Y-%m-%d %H:%M:%S") if file_meta.modified_at else "N/A"
        )

        table.add_row("Path", file_meta.path)
        table.add_row("Size", f"{file_meta.size:,} bytes")
        table.add_row("Created", created_str)
        table.add_row("Modified", modified_str)
        table.add_row("ETag", file_meta.etag or "N/A")
        table.add_row("MIME Type", file_meta.mime_type or "N/A")

        console.print(table)
    except Exception as e:
        handle_error(e)


@main.command()
@CONFIG_OPTION
@DATA_DIR_OPTION
def version(config: str | None, data_dir: str) -> None:  # noqa: ARG001
    """Show Nexus version information."""
    _ = config  # Unused but required by decorator
    console.print(f"[cyan]Nexus[/cyan] version [green]{nexus.__version__}[/green]")
    console.print(f"Data directory: [cyan]{data_dir}[/cyan]")


if __name__ == "__main__":
    main()
