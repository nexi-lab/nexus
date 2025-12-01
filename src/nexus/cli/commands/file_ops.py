"""File operation commands - read, write, cat, cp, mv, rm, sync."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
from rich.syntax import Syntax

import nexus
from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    add_context_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register all file operation commands."""
    cli.add_command(init)
    cli.add_command(cat)
    cli.add_command(write)
    cli.add_command(append)
    cli.add_command(write_batch)
    cli.add_command(cp)
    cli.add_command(copy_cmd)
    cli.add_command(move_cmd)
    cli.add_command(sync_cmd)
    cli.add_command(rm)


@click.command()
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


@click.command()
@click.argument("path", type=str)
@click.option(
    "--metadata",
    is_flag=True,
    help="Show file metadata (etag, version) for optimistic concurrency control",
)
@click.option(
    "--at-operation",
    type=str,
    help="Read file content at a historical operation point (time-travel debugging)",
)
@add_backend_options
@add_context_options
def cat(
    path: str,
    metadata: bool,
    at_operation: str | None,
    backend_config: BackendConfig,
    operation_context: dict[str, Any],
) -> None:
    """Display file contents.

    Examples:
        # Display file content
        nexus cat /workspace/data.txt

        # Display with syntax highlighting
        nexus cat /workspace/code.py

        # Show metadata (etag, version) for OCC
        nexus cat /workspace/data.txt --metadata

        # Time-travel: Read file at historical operation point
        nexus cat /workspace/data.txt --at-operation op_abc123
    """
    try:
        nx = get_filesystem(backend_config)

        if at_operation:
            # Time-travel: Read file at historical operation point
            # Import at function level to avoid scoping issues
            try:
                from nexus.core.nexus_fs import NexusFS
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

                # Get file state at operation
                state = time_travel.get_file_at_operation(
                    path, at_operation, tenant_id=nx.tenant_id
                )

            nx.close()

            # Display time-travel info
            console.print("[bold cyan]Time-Travel Mode[/bold cyan]")
            console.print(f"[dim]Operation ID:[/dim]  {state['operation_id']}")
            console.print(f"[dim]Operation Time:[/dim] {state['operation_time']}")
            console.print()

            if metadata:
                console.print("[bold]Metadata:[/bold]")
                console.print(f"[dim]Path:[/dim]     {path}")
                console.print(f"[dim]Size:[/dim]     {state['metadata'].get('size', 0)} bytes")
                console.print(f"[dim]Owner:[/dim]    {state['metadata'].get('owner', '-')}")
                console.print(f"[dim]Group:[/dim]    {state['metadata'].get('group', '-')}")
                console.print(f"[dim]Mode:[/dim]     {state['metadata'].get('mode', '-')}")
                console.print(f"[dim]Modified:[/dim] {state['metadata'].get('modified_at', '-')}")
                console.print()
                console.print("[bold]Content:[/bold]")

            content = state["content"]
        elif metadata:
            # Read with metadata for OCC
            data = nx.read(path, context=operation_context, return_metadata=True)
            nx.close()

            # Type narrowing: when return_metadata=True, result is always dict
            assert isinstance(data, dict), "Expected dict when return_metadata=True"

            # Display metadata first
            console.print("[bold]Metadata:[/bold]")
            console.print(f"[dim]Path:[/dim]     {path}")
            console.print(f"[dim]ETag:[/dim]     {data['etag']}")
            console.print(f"[dim]Version:[/dim]  {data['version']}")
            console.print(f"[dim]Size:[/dim]     {data['size']} bytes")
            console.print(f"[dim]Modified:[/dim] {data['modified_at']}")
            console.print()
            console.print("[bold]Content:[/bold]")
            content = data["content"]
        else:
            # Check file size to decide between read() and stream()
            # Stream large files (>10MB) to avoid memory exhaustion
            STREAM_THRESHOLD = 10 * 1024 * 1024  # 10MB
            file_size = 0

            # Try to get file size for local filesystems
            if hasattr(nx, "metadata"):
                try:
                    meta = nx.metadata.get(path)
                    file_size = meta.size if meta else 0
                except Exception:
                    # Fall back to reading without size check
                    file_size = 0

            if file_size > STREAM_THRESHOLD:
                # Stream large file in chunks (memory-efficient)
                import sys

                console.print(f"[dim]Streaming large file ({file_size:,} bytes)...[/dim]")
                try:
                    for chunk in nx.stream(  # type: ignore[attr-defined]
                        path, chunk_size=65536, context=operation_context
                    ):  # 64KB chunks
                        sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    nx.close()
                    return
                except Exception as e:
                    nx.close()
                    raise e
            else:
                # Read normally for small files
                content = nx.read(path, context=operation_context)
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


@click.command()
@click.argument("path", type=str)
@click.argument("content", type=str, required=False)
@click.option("-i", "--input", "input_file", type=click.File("rb"), help="Read from file or stdin")
@click.option(
    "--if-match",
    type=str,
    help="Only write if current ETag matches (optimistic concurrency control)",
)
@click.option(
    "--if-none-match",
    is_flag=True,
    help="Only write if file doesn't exist (create-only mode)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force overwrite without version check (dangerous - can cause data loss!)",
)
@click.option(
    "--show-metadata",
    is_flag=True,
    help="Show metadata (etag, version) after writing",
)
@add_backend_options
@add_context_options
def write(
    path: str,
    content: str | None,
    input_file: Any,
    if_match: str | None,
    if_none_match: bool,
    force: bool,
    show_metadata: bool,
    backend_config: BackendConfig,
    operation_context: dict[str, Any],
) -> None:
    """Write content to a file with optional optimistic concurrency control.

    Examples:
        # Simple write
        nexus write /workspace/data.txt "Hello World"

        # Write from stdin
        echo "Hello World" | nexus write /workspace/data.txt --input -

        # Write from file
        nexus write /workspace/data.txt --input local_file.txt

        # Optimistic concurrency control (prevent overwriting concurrent changes)
        nexus write /doc.txt "Updated content" --if-match abc123...

        # Create-only mode (fail if file exists)
        nexus write /new.txt "Initial content" --if-none-match

        # Show metadata after writing
        nexus write /doc.txt "Content" --show-metadata
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

        # Write with OCC parameters and context
        result = nx.write(
            path,
            file_content,
            context=operation_context,
            if_match=if_match,
            if_none_match=if_none_match,
            force=force,
        )
        nx.close()

        console.print(f"[green]✓[/green] Wrote {len(file_content)} bytes to [cyan]{path}[/cyan]")

        if show_metadata:
            console.print(f"[dim]ETag:[/dim]     {result['etag']}")
            console.print(f"[dim]Version:[/dim]  {result['version']}")
            console.print(f"[dim]Size:[/dim]     {result['size']} bytes")
            console.print(f"[dim]Modified:[/dim] {result['modified_at']}")
    except Exception as e:
        handle_error(e)


@click.command()
@click.argument("path", type=str)
@click.argument("content", type=str, required=False)
@click.option("-i", "--input", "input_file", type=click.File("rb"), help="Read from file or stdin")
@click.option(
    "--if-match",
    type=str,
    help="Only append if current ETag matches (optimistic concurrency control)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force append without version check (dangerous - can cause data loss!)",
)
@click.option(
    "--show-metadata",
    is_flag=True,
    help="Show metadata (etag, version) after appending",
)
@add_backend_options
@add_context_options
def append(
    path: str,
    content: str | None,
    input_file: Any,
    if_match: str | None,
    force: bool,
    show_metadata: bool,
    backend_config: BackendConfig,
    operation_context: dict[str, Any],
) -> None:
    """Append content to a file (creates file if it doesn't exist).

    This is useful for building log files, JSONL files, and other
    append-only data structures without reading the entire file first.

    Examples:
        # Append to a log file
        nexus append /workspace/app.log "New log entry\\n"

        # Append from stdin (useful for streaming)
        echo "New line" | nexus append /workspace/data.txt --input -

        # Append from file
        nexus append /workspace/output.txt --input input.txt

        # Build JSONL file incrementally
        echo '{"event": "login", "user": "alice"}' | nexus append /logs/events.jsonl --input -

        # Optimistic concurrency control (prevent concurrent modifications)
        nexus append /doc.txt "New content" --if-match abc123...

        # Show metadata after appending
        nexus append /log.txt "Entry\\n" --show-metadata
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

        # Append with OCC parameters and context
        result = nx.append(
            path,
            file_content,
            context=operation_context,
            if_match=if_match,
            force=force,
        )
        nx.close()

        console.print(f"[green]✓[/green] Appended {len(file_content)} bytes to [cyan]{path}[/cyan]")

        if show_metadata:
            console.print(f"[dim]ETag:[/dim]     {result['etag']}")
            console.print(f"[dim]Version:[/dim]  {result['version']}")
            console.print(f"[dim]Size:[/dim]     {result['size']} bytes")
            console.print(f"[dim]Modified:[/dim] {result['modified_at']}")
    except Exception as e:
        handle_error(e)


@click.command(name="write-batch")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option(
    "--dest-prefix",
    type=str,
    default="/",
    help="Destination prefix path in Nexus (default: /)",
)
@click.option(
    "--pattern",
    type=str,
    default="**/*",
    help="Glob pattern to filter files (default: **/* for all files)",
)
@click.option(
    "--exclude",
    type=str,
    multiple=True,
    help="Exclude patterns (can be specified multiple times)",
)
@click.option(
    "--show-progress",
    is_flag=True,
    default=True,
    help="Show progress during upload",
)
@click.option(
    "--batch-size",
    type=int,
    default=100,
    help="Number of files to write in each batch (default: 100)",
)
@add_backend_options
def write_batch(
    source_dir: str,
    dest_prefix: str,
    pattern: str,
    exclude: tuple[str, ...],
    show_progress: bool,
    batch_size: int,
    backend_config: BackendConfig,
) -> None:
    """Write multiple files to Nexus in batches for improved performance.

    This command uses the batch write API which is 4x faster than individual
    writes for many small files. It uploads all files from a local directory
    to Nexus while preserving directory structure.

    Examples:
        # Upload entire directory to root
        nexus write-batch ./my-data

        # Upload to specific destination prefix
        nexus write-batch ./logs --dest-prefix /workspace/logs

        # Upload only text files
        nexus write-batch ./docs --pattern "**/*.txt"

        # Exclude certain patterns
        nexus write-batch ./src --exclude "*.pyc" --exclude "__pycache__/*"

        # Use larger batch size for better performance
        nexus write-batch ./checkpoints --batch-size 200
    """
    try:
        import time

        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        nx = get_filesystem(backend_config)
        source_path = Path(source_dir)

        # Ensure dest_prefix starts with /
        if not dest_prefix.startswith("/"):
            dest_prefix = "/" + dest_prefix

        # Collect all files matching the pattern
        console.print(f"[cyan]Scanning[/cyan] {source_path} for files...")
        all_files = list(source_path.glob(pattern))

        # Filter out directories and excluded patterns
        files_to_upload: list[Path] = []
        for file_path in all_files:
            if not file_path.is_file():
                continue

            # Check exclude patterns
            excluded = False
            for exclude_pattern in exclude:
                if file_path.match(exclude_pattern):
                    excluded = True
                    break

            if not excluded:
                files_to_upload.append(file_path)

        if not files_to_upload:
            console.print("[yellow]No files found matching criteria[/yellow]")
            nx.close()
            return

        console.print(f"[cyan]Found {len(files_to_upload)} files to upload[/cyan]")

        # Process files in batches
        total_bytes = 0
        total_files = 0
        start_time = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total} files)"),
            TimeElapsedColumn(),
            console=console,
            disable=not show_progress,
        ) as progress:
            task = progress.add_task("Uploading files...", total=len(files_to_upload))

            for i in range(0, len(files_to_upload), batch_size):
                batch = files_to_upload[i : i + batch_size]
                batch_data: list[tuple[str, bytes]] = []

                # Prepare batch
                for file_path in batch:
                    # Calculate relative path from source_dir
                    rel_path = file_path.relative_to(source_path)
                    # Create destination path
                    dest_path = f"{dest_prefix.rstrip('/')}/{rel_path.as_posix()}"

                    # Read file content
                    content = file_path.read_bytes()
                    batch_data.append((dest_path, content))
                    total_bytes += len(content)

                # Write batch
                nx.write_batch(batch_data)
                total_files += len(batch_data)

                # Update progress
                progress.update(task, advance=len(batch_data))

        elapsed_time = time.time() - start_time
        nx.close()

        # Display summary
        console.print()
        console.print("[green]✓ Batch upload complete![/green]")
        console.print(f"  Files uploaded:  [cyan]{total_files}[/cyan]")
        console.print(f"  Total size:      [cyan]{total_bytes:,}[/cyan] bytes")
        console.print(f"  Time elapsed:    [cyan]{elapsed_time:.2f}[/cyan] seconds")
        if elapsed_time > 0:
            files_per_sec = total_files / elapsed_time
            mb_per_sec = (total_bytes / 1024 / 1024) / elapsed_time
            console.print(f"  Throughput:      [cyan]{files_per_sec:.1f}[/cyan] files/sec")
            console.print(f"  Bandwidth:       [cyan]{mb_per_sec:.2f}[/cyan] MB/sec")

    except Exception as e:
        handle_error(e)


@click.command()
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

        # Type narrowing: when return_metadata=False (default), result is bytes
        assert isinstance(content, bytes), "Expected bytes from read()"

        # Write to destination
        nx.write(dest, content)

        nx.close()

        console.print(f"[green]✓[/green] Copied [cyan]{source}[/cyan] → [cyan]{dest}[/cyan]")
    except Exception as e:
        handle_error(e)


@click.command(name="copy")
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


@click.command(name="move")
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


@click.command(name="sync")
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


@click.command()
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
