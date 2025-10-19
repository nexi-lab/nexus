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
from nexus import NexusFilesystem, NexusFS
from nexus.core.exceptions import NexusError, NexusFileNotFoundError, ValidationError

console = Console()

# Global options
BACKEND_OPTION = click.option(
    "--backend",
    type=click.Choice(["local", "gcs"]),
    default="local",
    help="Backend type: local (default) or gcs (Google Cloud Storage)",
    show_default=True,
)

DATA_DIR_OPTION = click.option(
    "--data-dir",
    type=click.Path(),
    default="./nexus-data",
    help="Path to Nexus data directory (for local backend and metadata DB)",
    show_default=True,
)

GCS_BUCKET_OPTION = click.option(
    "--gcs-bucket",
    type=str,
    default=None,
    help="GCS bucket name (required when backend=gcs)",
)

GCS_PROJECT_OPTION = click.option(
    "--gcs-project",
    type=str,
    default=None,
    help="GCP project ID (optional for GCS backend)",
)

GCS_CREDENTIALS_OPTION = click.option(
    "--gcs-credentials",
    type=click.Path(exists=True),
    default=None,
    help="Path to GCS service account credentials JSON file",
)

CONFIG_OPTION = click.option(
    "--config",
    type=click.Path(exists=True),
    default=None,
    help="Path to Nexus config file (nexus.yaml)",
)


class BackendConfig:
    """Configuration for backend connection."""

    def __init__(
        self,
        backend: str = "local",
        data_dir: str = "./nexus-data",
        config_path: str | None = None,
        gcs_bucket: str | None = None,
        gcs_project: str | None = None,
        gcs_credentials: str | None = None,
    ):
        self.backend = backend
        self.data_dir = data_dir
        self.config_path = config_path
        self.gcs_bucket = gcs_bucket
        self.gcs_project = gcs_project
        self.gcs_credentials = gcs_credentials


def add_backend_options(func: Any) -> Any:
    """Decorator to add all backend-related options to a command and pass them via context."""
    import functools

    @CONFIG_OPTION
    @BACKEND_OPTION
    @DATA_DIR_OPTION
    @GCS_BUCKET_OPTION
    @GCS_PROJECT_OPTION
    @GCS_CREDENTIALS_OPTION
    @functools.wraps(func)
    def wrapper(
        config: str | None,
        backend: str,
        data_dir: str,
        gcs_bucket: str | None,
        gcs_project: str | None,
        gcs_credentials: str | None,
        **kwargs: Any,
    ) -> Any:
        # Create backend config and pass to function
        backend_config = BackendConfig(
            backend=backend,
            data_dir=data_dir,
            config_path=config,
            gcs_bucket=gcs_bucket,
            gcs_project=gcs_project,
            gcs_credentials=gcs_credentials,
        )
        return func(backend_config=backend_config, **kwargs)

    return wrapper


def get_filesystem(backend_config: BackendConfig) -> NexusFilesystem:
    """Get Nexus filesystem instance from backend configuration."""
    try:
        if backend_config.config_path:
            # Use explicit config file
            return nexus.connect(config=backend_config.config_path)
        elif backend_config.backend == "gcs":
            # Use GCS backend via nexus.connect()
            if not backend_config.gcs_bucket:
                console.print("[red]Error:[/red] --gcs-bucket is required when using --backend=gcs")
                sys.exit(1)
            config = {
                "backend": "gcs",
                "gcs_bucket_name": backend_config.gcs_bucket,
                "gcs_project_id": backend_config.gcs_project,
                "gcs_credentials_path": backend_config.gcs_credentials,
                "db_path": str(Path(backend_config.data_dir) / "nexus-gcs-metadata.db"),
            }
            return nexus.connect(config=config)
        else:
            # Use local backend (default)
            return nexus.connect(config={"data_dir": backend_config.data_dir})
    except Exception as e:
        console.print(f"[red]Error connecting to Nexus:[/red] {e}")
        sys.exit(1)


def handle_error(e: Exception) -> None:
    """Handle errors with beautiful output."""
    if isinstance(e, NexusFileNotFoundError):
        console.print(f"[red]Error:[/red] File not found: {e}")
    elif isinstance(e, ValidationError):
        console.print(f"[red]Validation Error:[/red] {e}")
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
@add_backend_options
def cat(
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Display file contents.

    Examples:
        nexus cat /workspace/data.txt
        nexus cat /workspace/code.py
    """
    try:
        nx = get_filesystem(backend_config)
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
@add_backend_options
def write(
    path: str,
    content: str | None,
    input_file: Any,
    backend_config: BackendConfig,
) -> None:
    """Write content to a file.

    Examples:
        nexus write /workspace/data.txt "Hello World"
        echo "Hello World" | nexus write /workspace/data.txt --input -
        nexus write /workspace/data.txt --input local_file.txt
    """
    try:
        nx = get_filesystem(backend_config)

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
@add_backend_options
def cp(
    source: str,
    dest: str,
    backend_config: BackendConfig,
) -> None:
    """Copy a file (simple copy - for recursive copy use 'copy' command).

    Examples:
        nexus cp /workspace/source.txt /workspace/dest.txt
    """
    try:
        nx = get_filesystem(backend_config)

        # Read source
        content = nx.read(source)

        # Write to destination
        nx.write(dest, content)

        nx.close()

        console.print(f"[green]✓[/green] Copied [cyan]{source}[/cyan] → [cyan]{dest}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command(name="copy")
@click.argument("source", type=str)
@click.argument("dest", type=str)
@click.option("-r", "--recursive", is_flag=True, help="Copy directories recursively")
@click.option("--checksum", is_flag=True, help="Skip identical files (hash-based)", default=True)
@click.option("--no-checksum", is_flag=True, help="Disable checksum verification")
@add_backend_options
def copy_cmd(
    source: str,
    dest: str,
    recursive: bool,
    checksum: bool,
    no_checksum: bool,
    backend_config: BackendConfig,
) -> None:
    """Smart copy with deduplication.

    Copy files from source to destination with automatic deduplication.
    Uses content hashing to skip identical files.

    Supports both local filesystem paths and Nexus paths:
    - /path/in/nexus - Nexus virtual path
    - ./local/path or /local/path - Local filesystem path

    Examples:
        # Copy local directory to Nexus
        nexus copy ./local/data/ /workspace/data/ --recursive

        # Copy within Nexus
        nexus copy /workspace/source/ /workspace/dest/ --recursive

        # Copy Nexus to local
        nexus copy /workspace/data/ ./backup/ --recursive

        # Copy single file
        nexus copy /workspace/file.txt /workspace/copy.txt
    """
    try:
        from nexus.sync import copy_file, copy_recursive, is_local_path

        nx = get_filesystem(backend_config)

        # Handle --no-checksum flag
        use_checksum = checksum and not no_checksum

        if recursive:
            # Use progress bar from sync module (tqdm)
            stats = copy_recursive(nx, source, dest, checksum=use_checksum, progress=True)
            nx.close()

            # Display results
            console.print("[bold green]✓ Copy Complete![/bold green]")
            console.print(f"  Files checked: [cyan]{stats.files_checked}[/cyan]")
            console.print(f"  Files copied: [green]{stats.files_copied}[/green]")
            console.print(f"  Files skipped: [yellow]{stats.files_skipped}[/yellow] (identical)")
            console.print(f"  Bytes transferred: [cyan]{stats.bytes_transferred:,}[/cyan]")

            if stats.errors:
                console.print(f"\n[bold red]Errors:[/bold red] {len(stats.errors)}")
                for error in stats.errors[:10]:  # Show first 10 errors
                    console.print(f"  [red]•[/red] {error}")

        else:
            # Single file copy
            is_source_local = is_local_path(source)
            is_dest_local = is_local_path(dest)

            bytes_copied = copy_file(nx, source, dest, is_source_local, is_dest_local, use_checksum)

            nx.close()

            if bytes_copied > 0:
                console.print(
                    f"[green]✓[/green] Copied [cyan]{source}[/cyan] → [cyan]{dest}[/cyan] "
                    f"({bytes_copied:,} bytes)"
                )
            else:
                console.print(
                    f"[yellow]⊘[/yellow] Skipped [cyan]{source}[/cyan] (identical content)"
                )

    except Exception as e:
        handle_error(e)


@main.command(name="move")
@click.argument("source", type=str)
@click.argument("dest", type=str)
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@add_backend_options
def move_cmd(
    source: str,
    dest: str,
    force: bool,
    backend_config: BackendConfig,
) -> None:
    """Move files or directories.

    Move files from source to destination. This is an efficient rename
    when possible, otherwise copy + delete.

    Examples:
        nexus move /workspace/old.txt /workspace/new.txt
        nexus move /workspace/old_dir/ /workspace/new_dir/ --force
    """
    try:
        from nexus.sync import move_file

        nx = get_filesystem(backend_config)

        # Confirm unless --force
        if not force and not click.confirm(f"Move {source} to {dest}?"):
            console.print("[yellow]Cancelled[/yellow]")
            nx.close()
            return

        with console.status(f"[yellow]Moving {source} to {dest}...[/yellow]", spinner="dots"):
            success = move_file(nx, source, dest)

        nx.close()

        if success:
            console.print(f"[green]✓[/green] Moved [cyan]{source}[/cyan] → [cyan]{dest}[/cyan]")
        else:
            console.print(f"[red]Error:[/red] Failed to move {source}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@main.command(name="sync")
@click.argument("source", type=str)
@click.argument("dest", type=str)
@click.option("--delete", is_flag=True, help="Delete files in dest that don't exist in source")
@click.option("--dry-run", is_flag=True, help="Preview changes without making them")
@click.option("--no-checksum", is_flag=True, help="Disable hash-based comparison")
@add_backend_options
def sync_cmd(
    source: str,
    dest: str,
    delete: bool,
    dry_run: bool,
    no_checksum: bool,
    backend_config: BackendConfig,
) -> None:
    """One-way sync from source to destination.

    Efficiently synchronizes files from source to destination using
    hash-based change detection. Only copies changed files.

    Supports both local filesystem paths and Nexus paths.

    Examples:
        # Sync local to Nexus
        nexus sync ./local/dataset/ /workspace/training/

        # Preview changes (dry run)
        nexus sync ./local/data/ /workspace/data/ --dry-run

        # Sync with deletion (mirror)
        nexus sync /workspace/source/ /workspace/dest/ --delete

        # Disable checksum (copy all files)
        nexus sync ./data/ /workspace/ --no-checksum
    """
    try:
        from nexus.sync import sync_directories

        nx = get_filesystem(backend_config)

        use_checksum = not no_checksum

        # Display sync configuration
        console.print(f"[cyan]Syncing:[/cyan] {source} → {dest}")
        if delete:
            console.print("  [yellow]⚠ Delete mode enabled[/yellow]")
        if dry_run:
            console.print("  [yellow]DRY RUN - No changes will be made[/yellow]")
        if not use_checksum:
            console.print("  [yellow]Checksum disabled - copying all files[/yellow]")
        console.print()

        # Use progress bar from sync module (tqdm)
        stats = sync_directories(
            nx, source, dest, delete=delete, dry_run=dry_run, checksum=use_checksum, progress=True
        )

        nx.close()

        # Display results
        if dry_run:
            console.print("[bold yellow]DRY RUN RESULTS:[/bold yellow]")
        else:
            console.print("[bold green]✓ Sync Complete![/bold green]")

        console.print(f"  Files checked: [cyan]{stats.files_checked}[/cyan]")
        console.print(f"  Files copied: [green]{stats.files_copied}[/green]")
        console.print(f"  Files skipped: [yellow]{stats.files_skipped}[/yellow] (identical)")

        if delete:
            console.print(f"  Files deleted: [red]{stats.files_deleted}[/red]")

        if not dry_run:
            console.print(f"  Bytes transferred: [cyan]{stats.bytes_transferred:,}[/cyan]")

        if stats.errors:
            console.print(f"\n[bold red]Errors:[/bold red] {len(stats.errors)}")
            for error in stats.errors[:10]:  # Show first 10 errors
                console.print(f"  [red]•[/red] {error}")

    except Exception as e:
        handle_error(e)


@main.command()
@click.argument("path", type=str)
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@add_backend_options
def rm(
    path: str,
    force: bool,
    backend_config: BackendConfig,
) -> None:
    """Delete a file.

    Examples:
        nexus rm /workspace/data.txt
        nexus rm /workspace/data.txt --force
    """
    try:
        nx = get_filesystem(backend_config)

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
@add_backend_options
def glob(
    pattern: str,
    path: str,
    backend_config: BackendConfig,
) -> None:
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
        nx = get_filesystem(backend_config)
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
@click.option(
    "--search-mode",
    type=click.Choice(["auto", "parsed", "raw"]),
    default="auto",
    help="Search mode: auto (try parsed, fallback to raw), parsed (only parsed), raw (only raw)",
    show_default=True,
)
@add_backend_options
def grep(
    pattern: str,
    path: str,
    file_pattern: str | None,
    ignore_case: bool,
    max_results: int,
    search_mode: str,
    backend_config: BackendConfig,
) -> None:
    """Search file contents using regex patterns.

    Search Modes:
    - auto: Try parsed text first, fallback to raw (default)
    - parsed: Only search parsed text (great for PDFs/docs)
    - raw: Only search raw file content (skip parsing)

    Examples:
        # Search all files (auto mode - tries parsed first)
        nexus grep "TODO"

        # Search only parsed content from PDFs
        nexus grep "revenue" --file-pattern "**/*.pdf" --search-mode=parsed

        # Search only raw content (skip parsing)
        nexus grep "TODO" --search-mode=raw

        # Other options
        nexus grep "def \\w+" --file-pattern "**/*.py"
        nexus grep "error" --ignore-case
        nexus grep "TODO" --path /workspace
    """
    try:
        nx = get_filesystem(backend_config)
        matches = nx.grep(
            pattern,
            path=path,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
            search_mode=search_mode,
        )
        nx.close()

        if not matches:
            console.print(f"[yellow]No matches found for:[/yellow] {pattern}")
            return

        console.print(f"[green]Found {len(matches)} matches[/green] for [cyan]{pattern}[/cyan]")
        console.print(f"[dim]Search mode: {search_mode}[/dim]\n")

        current_file = None
        for match in matches:
            if match["file"] != current_file:
                current_file = match["file"]
                console.print(f"[bold cyan]{current_file}[/bold cyan]")

            # Display source type
            source = match.get("source", "raw")
            source_color = "magenta" if source == "parsed" else "dim"
            console.print(f"  [yellow]{match['line']}:[/yellow] {match['content']}")
            console.print(
                f"      [dim]Match: [green]{match['match']}[/green] "
                f"[{source_color}]({source})[/{source_color}][/dim]"
            )
    except Exception as e:
        handle_error(e)


@main.command()
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


@main.command()
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


@main.command()
@click.argument("path", type=str)
@add_backend_options
def info(
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Show detailed file information.

    Examples:
        nexus info /workspace/data.txt
    """
    try:
        nx = get_filesystem(backend_config)

        # Check if file exists first
        if not nx.exists(path):
            console.print(f"[yellow]File not found:[/yellow] {path}")
            nx.close()
            return

        # Get file metadata from metadata store
        # Note: Only NexusFS mode has direct metadata access
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] File info is only available for NexusFS instances")
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
@add_backend_options
def version(
    backend_config: BackendConfig,
) -> None:  # noqa: ARG001
    """Show Nexus version information."""
    console.print(f"[cyan]Nexus[/cyan] version [green]{nexus.__version__}[/green]")
    console.print(f"Data directory: [cyan]{backend_config.data_dir}[/cyan]")


@main.command(name="export")
@click.argument("output", type=click.Path())
@click.option("-p", "--prefix", default="", help="Export only files with this prefix")
@click.option("--tenant-id", default=None, help="Filter by tenant ID")
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
    tenant_id: str | None,
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
        nexus export tenant.jsonl --tenant-id acme-corp
    """
    try:
        from nexus.core.export_import import ExportFilter

        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports metadata export
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] Metadata export is only available in embedded mode")
            nx.close()
            sys.exit(1)

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
            tenant_id=tenant_id,
            path_prefix=prefix,
            after_time=after_time,
            include_deleted=include_deleted,
        )

        # Display filter options
        console.print(f"[cyan]Exporting metadata to:[/cyan] {output}")
        if prefix:
            console.print(f"  Path prefix: [cyan]{prefix}[/cyan]")
        if tenant_id:
            console.print(f"  Tenant ID: [cyan]{tenant_id}[/cyan]")
        if after_time:
            console.print(f"  After time: [cyan]{after_time.isoformat()}[/cyan]")
        if include_deleted:
            console.print("  [yellow]Including deleted files[/yellow]")

        with console.status("[yellow]Exporting metadata...[/yellow]", spinner="dots"):
            count = nx.export_metadata(output, filter=export_filter)

        nx.close()

        console.print(f"[green]✓[/green] Exported [cyan]{count}[/cyan] file metadata records")
        console.print(f"  Output: [cyan]{output}[/cyan]")
    except Exception as e:
        handle_error(e)


@main.command(name="import")
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
@click.option(
    "--overwrite",
    is_flag=True,
    hidden=True,
    help="(Deprecated) Use --conflict-mode=overwrite instead",
)
@click.option(
    "--no-skip-existing",
    is_flag=True,
    hidden=True,
    help="(Deprecated) Use --conflict-mode option instead",
)
@add_backend_options
def import_metadata(
    input_file: str,
    conflict_mode: str,
    dry_run: bool,
    no_preserve_ids: bool,
    overwrite: bool,
    no_skip_existing: bool,
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
        from nexus.core.export_import import ImportOptions

        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports metadata import
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] Metadata import is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Handle deprecated options for backward compatibility
        _ = no_skip_existing  # Deprecated parameter, kept for backward compatibility

        if overwrite:
            console.print(
                "[yellow]Warning:[/yellow] --overwrite is deprecated, use --conflict-mode=overwrite"
            )
            conflict_mode = "overwrite"

        # Create import options
        import_options = ImportOptions(
            dry_run=dry_run,
            conflict_mode=conflict_mode,  # type: ignore
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
            result = nx.import_metadata(input_file, options=import_options)

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


@main.command(name="work")
@click.argument(
    "view_type",
    type=click.Choice(["ready", "pending", "blocked", "in-progress", "status"]),
)
@click.option("-l", "--limit", type=int, default=None, help="Maximum number of results to show")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@add_backend_options
def work_command(
    view_type: str,
    limit: int | None,
    json_output: bool,
    backend_config: BackendConfig,
) -> None:
    """Query work items using SQL views.

    View Types:
    - ready: Files ready for processing (status='ready', no blockers)
    - pending: Files waiting to be processed (status='pending')
    - blocked: Files blocked by dependencies
    - in-progress: Files currently being processed
    - status: Show aggregate statistics of all work queues

    Examples:
        nexus work ready --limit 10
        nexus work blocked
        nexus work status
        nexus work ready --json
    """
    try:
        nx = get_filesystem(backend_config)

        # Only Embedded mode has metadata store with work views
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] Work views are only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Handle status view (aggregate statistics)
        if view_type == "status":
            if json_output:
                import json

                ready_count = len(nx.metadata.get_ready_work())
                pending_count = len(nx.metadata.get_pending_work())
                blocked_count = len(nx.metadata.get_blocked_work())
                in_progress_count = len(nx.metadata.get_in_progress_work())

                status_data = {
                    "ready": ready_count,
                    "pending": pending_count,
                    "blocked": blocked_count,
                    "in_progress": in_progress_count,
                    "total": ready_count + pending_count + blocked_count + in_progress_count,
                }
                console.print(json.dumps(status_data, indent=2))
            else:
                ready_count = len(nx.metadata.get_ready_work())
                pending_count = len(nx.metadata.get_pending_work())
                blocked_count = len(nx.metadata.get_blocked_work())
                in_progress_count = len(nx.metadata.get_in_progress_work())
                total_count = ready_count + pending_count + blocked_count + in_progress_count

                table = Table(title="Work Queue Status")
                table.add_column("Queue", style="cyan")
                table.add_column("Count", justify="right", style="green")

                table.add_row("Ready", str(ready_count))
                table.add_row("Pending", str(pending_count))
                table.add_row("Blocked", str(blocked_count))
                table.add_row("In Progress", str(in_progress_count))
                table.add_row("[bold]Total[/bold]", f"[bold]{total_count}[/bold]")

                console.print(table)

            nx.close()
            return

        # Get work items based on view type
        if view_type == "ready":
            items = nx.metadata.get_ready_work(limit=limit)
            title = "Ready Work Items"
            description = "Files ready for processing"
        elif view_type == "pending":
            items = nx.metadata.get_pending_work(limit=limit)
            title = "Pending Work Items"
            description = "Files waiting to be processed"
        elif view_type == "blocked":
            items = nx.metadata.get_blocked_work(limit=limit)
            title = "Blocked Work Items"
            description = "Files blocked by dependencies"
        elif view_type == "in-progress":
            items = nx.metadata.get_in_progress_work(limit=limit)
            title = "In-Progress Work Items"
            description = "Files currently being processed"
        else:
            console.print(f"[red]Error:[/red] Unknown view type: {view_type}")
            nx.close()
            sys.exit(1)

        nx.close()

        # Output results
        if not items:
            console.print(f"[yellow]No {view_type} work items found[/yellow]")
            return

        if json_output:
            import json

            console.print(json.dumps(items, indent=2, default=str))
        else:
            console.print(f"[green]{description}[/green] ([cyan]{len(items)}[/cyan] items)\n")

            table = Table(title=title)
            table.add_column("Path", style="cyan", no_wrap=False)
            table.add_column("Status", style="yellow")
            table.add_column("Priority", justify="right", style="green")

            # Add blocker_count column for blocked view
            if view_type == "blocked":
                table.add_column("Blockers", justify="right", style="red")

            # Add worker info for in-progress view
            if view_type == "in-progress":
                table.add_column("Worker ID", style="magenta")
                table.add_column("Started At", style="dim")

            for item in items:
                import json as json_lib

                # Extract status and priority
                status_value = "N/A"
                if item.get("status"):
                    try:
                        status_value = json_lib.loads(item["status"])
                    except (json_lib.JSONDecodeError, TypeError):
                        status_value = str(item["status"])

                priority_value = "N/A"
                if item.get("priority"):
                    try:
                        priority_value = str(json_lib.loads(item["priority"]))
                    except (json_lib.JSONDecodeError, TypeError):
                        priority_value = str(item["priority"])

                # Build row data
                row_data = [
                    item["virtual_path"],
                    status_value,
                    priority_value,
                ]

                # Add blocker count for blocked view
                if view_type == "blocked":
                    blocker_count = item.get("blocker_count", 0)
                    row_data.append(str(blocker_count))

                # Add worker info for in-progress view
                if view_type == "in-progress":
                    worker_id = "N/A"
                    if item.get("worker_id"):
                        try:
                            worker_id = json_lib.loads(item["worker_id"])
                        except (json_lib.JSONDecodeError, TypeError):
                            worker_id = str(item["worker_id"])

                    started_at = "N/A"
                    if item.get("started_at"):
                        try:
                            started_at = json_lib.loads(item["started_at"])
                        except (json_lib.JSONDecodeError, TypeError):
                            started_at = str(item["started_at"])

                    row_data.extend([worker_id, started_at])

                table.add_row(*row_data)

            console.print(table)

    except Exception as e:
        handle_error(e)


@main.command(name="find-duplicates")
@click.option("-p", "--path", default="/", help="Base path to search from")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@add_backend_options
def find_duplicates(path: str, json_output: bool, backend_config: BackendConfig) -> None:
    """Find duplicate files using content hashes.

    Uses batch_get_content_ids() for efficient deduplication detection.
    Groups files by their content hash to find duplicates.

    Examples:
        nexus find-duplicates
        nexus find-duplicates --path /workspace
        nexus find-duplicates --json
    """
    try:
        nx = get_filesystem(backend_config)

        # Only Embedded mode supports batch_get_content_ids
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] find-duplicates is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Get all files under path
        with console.status(f"[yellow]Scanning files in {path}...[/yellow]", spinner="dots"):
            all_files_raw = nx.list(path, recursive=True)

            # Check if we got detailed results (list of dicts) or simple paths (list of strings)
            if all_files_raw and isinstance(all_files_raw[0], dict):
                # details=True was used
                all_files_detailed = cast(list[dict[str, Any]], all_files_raw)
                file_paths = [f["path"] for f in all_files_detailed]
            else:
                # Simple list of paths
                file_paths = cast(list[str], all_files_raw)

        if not file_paths:
            console.print(f"[yellow]No files found in {path}[/yellow]")
            nx.close()
            return

        # Get content hashes in batch (single query)
        with console.status(
            f"[yellow]Analyzing {len(file_paths)} files for duplicates...[/yellow]",
            spinner="dots",
        ):
            content_ids = nx.batch_get_content_ids(file_paths)

            # Group by hash
            from collections import defaultdict

            by_hash = defaultdict(list)
            for file_path, content_hash in content_ids.items():
                if content_hash:
                    by_hash[content_hash].append(file_path)

            # Find duplicate groups (hash with >1 file)
            duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}

        nx.close()

        # Calculate statistics
        total_files = len(file_paths)
        unique_hashes = len(by_hash)
        duplicate_groups = len(duplicates)
        duplicate_files = sum(len(paths) for paths in duplicates.values())

        if json_output:
            import json

            result = {
                "total_files": total_files,
                "unique_hashes": unique_hashes,
                "duplicate_groups": duplicate_groups,
                "duplicate_files": duplicate_files,
                "duplicates": [
                    {"content_hash": h, "paths": paths} for h, paths in duplicates.items()
                ],
            }
            console.print(json.dumps(result, indent=2))
        else:
            # Display summary
            console.print("\n[bold cyan]Duplicate File Analysis[/bold cyan]")
            console.print(f"Total files scanned: [green]{total_files}[/green]")
            console.print(f"Unique content hashes: [green]{unique_hashes}[/green]")
            console.print(f"Duplicate groups: [yellow]{duplicate_groups}[/yellow]")
            console.print(f"Duplicate files: [yellow]{duplicate_files}[/yellow]")

            if not duplicates:
                console.print("\n[green]✓ No duplicate files found![/green]")
                return

            # Display duplicate groups
            console.print("\n[bold yellow]Duplicate Groups:[/bold yellow]\n")

            for i, (content_hash, paths) in enumerate(duplicates.items(), 1):
                console.print(f"[bold]Group {i}[/bold] (hash: [dim]{content_hash[:16]}...[/dim])")
                console.print(f"  [yellow]{len(paths)} files with identical content:[/yellow]")
                for path in sorted(paths):
                    console.print(f"    • {path}")
                console.print()

            # Calculate potential space savings
            # Each duplicate group can save (n-1) copies
            console.print("[bold cyan]Storage Impact:[/bold cyan]")
            console.print(
                f"  Files that could be deduplicated: [yellow]{duplicate_files - duplicate_groups}[/yellow]"
            )
            console.print("  (CAS automatically deduplicates - no action needed!)")

    except Exception as e:
        handle_error(e)


@main.command(name="tree")
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


@main.command(name="size")
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
            files_raw = nx.list(path, recursive=True, details=True)

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


@main.command(name="mount")
@click.argument("mount_point", type=click.Path())
@click.option(
    "--mode",
    type=click.Choice(["binary", "text", "smart"]),
    default="smart",
    help="Mount mode: binary (raw), text (parsed), smart (auto-detect)",
    show_default=True,
)
@click.option(
    "--daemon",
    is_flag=True,
    help="Run in background (daemon mode)",
)
@click.option(
    "--allow-other",
    is_flag=True,
    help="Allow other users to access the mount",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable FUSE debug output",
)
@add_backend_options
def mount(
    mount_point: str,
    mode: str,
    daemon: bool,
    allow_other: bool,
    debug: bool,
    backend_config: BackendConfig,
) -> None:
    """Mount Nexus filesystem to a local path.

    Mounts the Nexus filesystem using FUSE, allowing standard Unix tools
    to work seamlessly with Nexus files.

    Mount Modes:
    - binary: Return raw file content (no parsing)
    - text: Parse all files and return text representation
    - smart (default): Auto-detect file type and return appropriate format

    Virtual File Views:
    - .raw/ directory: Access original binary content
    - .txt suffix: View parsed text representation
    - .md suffix: View formatted markdown representation

    Examples:
        # Mount in smart mode (default)
        nexus mount /mnt/nexus

        # Mount in binary mode (raw files only)
        nexus mount /mnt/nexus --mode=binary

        # Mount in background
        nexus mount /mnt/nexus --daemon

        # Mount with debug output
        nexus mount /mnt/nexus --debug

        # Use standard Unix tools
        ls /mnt/nexus
        cat /mnt/nexus/workspace/document.pdf.txt
        grep "TODO" /mnt/nexus/workspace/**/*.py
        vim /mnt/nexus/workspace/file.txt
    """
    try:
        from nexus.fuse import mount_nexus

        # Get filesystem instance
        nx = get_filesystem(backend_config)

        # Create mount point if it doesn't exist
        mount_path = Path(mount_point)
        if not mount_path.exists():
            mount_path.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]✓[/green] Created mount point [cyan]{mount_point}[/cyan]")

        # Display mount info
        console.print("[green]Mounting Nexus filesystem...[/green]")
        console.print(f"  Mount point: [cyan]{mount_point}[/cyan]")
        console.print(f"  Mode: [cyan]{mode}[/cyan]")
        console.print(f"  Backend: [cyan]{backend_config.backend}[/cyan]")
        if daemon:
            console.print("  [yellow]Running in background (daemon mode)[/yellow]")

        console.print()
        console.print("[bold cyan]Virtual File Views:[/bold cyan]")
        console.print("  • [cyan].raw/[/cyan] - Access original binary content")
        console.print("  • [cyan]file.txt[/cyan] - View parsed text representation")
        console.print("  • [cyan]file.md[/cyan] - View formatted markdown")
        console.print()

        # Mount filesystem
        fuse = mount_nexus(
            nx,
            mount_point,
            mode=mode,
            foreground=not daemon,
            allow_other=allow_other,
            debug=debug,
        )

        if daemon:
            console.print(f"[green]✓[/green] Mounted Nexus to [cyan]{mount_point}[/cyan]")
            console.print()
            console.print("[yellow]To unmount:[/yellow]")
            console.print(f"  nexus unmount {mount_point}")
        else:
            console.print(f"[green]Mounted Nexus to [cyan]{mount_point}[/cyan][/green]")
            console.print("[yellow]Press Ctrl+C to unmount[/yellow]")

            # Wait for signal (foreground mode)
            try:
                fuse.wait()
            except KeyboardInterrupt:
                console.print("\n[yellow]Unmounting...[/yellow]")
                fuse.unmount()
                console.print("[green]✓[/green] Unmounted")

    except ImportError:
        console.print(
            "[red]Error:[/red] FUSE support not available. "
            "Install with: pip install 'nexus-ai-fs[fuse]'"
        )
        sys.exit(1)
    except Exception as e:
        handle_error(e)


@main.command(name="unmount")
@click.argument("mount_point", type=click.Path(exists=True))
def unmount(mount_point: str) -> None:
    """Unmount a Nexus filesystem.

    Examples:
        nexus unmount /mnt/nexus
    """
    try:
        import platform
        import subprocess

        system = platform.system()

        console.print(f"[yellow]Unmounting {mount_point}...[/yellow]")

        try:
            if system == "Darwin":  # macOS
                subprocess.run(
                    ["umount", mount_point],
                    check=True,
                    capture_output=True,
                )
            elif system == "Linux":
                subprocess.run(
                    ["fusermount", "-u", mount_point],
                    check=True,
                    capture_output=True,
                )
            else:
                console.print(f"[red]Error:[/red] Unsupported platform: {system}")
                sys.exit(1)

            console.print(f"[green]✓[/green] Unmounted [cyan]{mount_point}[/cyan]")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            console.print(f"[red]Error:[/red] Failed to unmount: {error_msg}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@main.command(name="serve")
@click.option("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
@click.option("--port", default=8080, type=int, help="Server port (default: 8080)")
@click.option("--api-key", default=None, help="API key for authentication (optional)")
@add_backend_options
def serve(
    host: str,
    port: int,
    api_key: str | None,
    backend_config: BackendConfig,
) -> None:
    """Start Nexus RPC server.

    Exposes all NexusFileSystem operations through a JSON-RPC API over HTTP.
    This allows remote clients (including FUSE mounts) to access Nexus over the network.

    The server provides direct endpoints for all NFS methods:
    - read, write, delete, exists
    - list, glob, grep
    - mkdir, rmdir, is_directory

    Examples:
        # Start server with local backend (no authentication)
        nexus serve

        # Start server with API key authentication
        nexus serve --api-key mysecretkey

        # Start server with GCS backend
        nexus serve --backend=gcs --gcs-bucket=my-bucket --api-key mysecretkey

        # Connect from Python
        from nexus.remote import RemoteNexusFS
        nx = RemoteNexusFS("http://localhost:8080", api_key="mysecretkey")
        nx.write("/workspace/file.txt", b"Hello, World!")

        # Mount with FUSE
        from nexus.fuse import mount_nexus
        mount_nexus(nx, "/mnt/nexus")
    """
    import logging

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        # Import server components
        from nexus.server.rpc_server import NexusRPCServer

        # Get filesystem instance
        nx = get_filesystem(backend_config)

        # Create and start server
        console.print("[green]Starting Nexus RPC server...[/green]")
        console.print(f"  Host: [cyan]{host}[/cyan]")
        console.print(f"  Port: [cyan]{port}[/cyan]")
        console.print(f"  Backend: [cyan]{backend_config.backend}[/cyan]")
        if backend_config.backend == "gcs":
            console.print(f"  GCS Bucket: [cyan]{backend_config.gcs_bucket}[/cyan]")
        else:
            console.print(f"  Data Dir: [cyan]{backend_config.data_dir}[/cyan]")

        if api_key:
            console.print("  Authentication: [yellow]API key required[/yellow]")
        else:
            console.print("  Authentication: [yellow]None (open access)[/yellow]")

        console.print()
        console.print("[bold cyan]Endpoints:[/bold cyan]")
        console.print(f"  Health check: [cyan]http://{host}:{port}/health[/cyan]")
        console.print(f"  RPC methods: [cyan]http://{host}:{port}/api/nfs/{{method}}[/cyan]")
        console.print()
        console.print("[yellow]Connect from Python:[/yellow]")
        console.print("  from nexus.remote import RemoteNexusFS")
        console.print(f'  nx = RemoteNexusFS("http://{host}:{port}"', end="")
        if api_key:
            console.print(f', api_key="{api_key}")')
        else:
            console.print(")")
        console.print("  nx.write('/workspace/file.txt', b'Hello!')")
        console.print()
        console.print("[green]Press Ctrl+C to stop server[/green]")

        server = NexusRPCServer(
            nexus_fs=nx,
            host=host,
            port=port,
            api_key=api_key,
        )

        server.serve_forever()

    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped by user[/yellow]")
    except Exception as e:
        handle_error(e)


if __name__ == "__main__":
    main()
