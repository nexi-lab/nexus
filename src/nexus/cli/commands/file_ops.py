"""File operation commands - read, write, cat, cp, mv, rm, sync."""

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import click
from rich.syntax import Syntax

from nexus.cli.dry_run import add_dry_run_option, dry_run_preview, render_dry_run
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    add_backend_options,
    add_context_options,
    connect_local_workspace,
    console,
    get_filesystem,
    handle_error,
    open_filesystem,
    resolve_content,
)


def register_commands(cli: click.Group) -> None:
    """Register all file operation commands.

    Note: ``init`` has been moved to ``init_cmd.py`` (Issue #2915).
    It is registered separately in ``__init__.py``.
    """
    cli.add_command(cat)
    cli.add_command(write)
    cli.add_command(append)
    cli.add_command(write_batch)
    cli.add_command(cp)
    cli.add_command(copy_cmd)
    cli.add_command(move_cmd)
    cli.add_command(sync_cmd)
    cli.add_command(rm)
    cli.add_command(edit)


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

    async def _impl() -> None:
        workspace_path = Path(path)
        data_dir = workspace_path / "nexus-data"

        try:
            # Create workspace structure
            workspace_path.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)

            # Initialize Nexus
            nx = connect_local_workspace(str(data_dir))

            # Create default directories
            nx.mkdir("/workspace", exist_ok=True)
            nx.mkdir("/shared", exist_ok=True)

            nx.close()

            console.print(
                f"[nexus.success]✓[/nexus.success] Initialized Nexus workspace at [nexus.path]{workspace_path}[/nexus.path]"
            )
            console.print(f"  Data directory: [nexus.path]{data_dir}[/nexus.path]")
            console.print(f"  Workspace: [nexus.path]{workspace_path / 'workspace'}[/nexus.path]")
            console.print(f"  Shared: [nexus.path]{workspace_path / 'shared'}[/nexus.path]")
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command()
@click.argument("path", type=str)
@click.option(
    "--metadata",
    is_flag=True,
    help="Show file metadata (content_id, version) for optimistic concurrency control",
)
@click.option(
    "--at-operation",
    type=str,
    help="Read file content at a historical operation point (time-travel debugging)",
)
@click.option(
    "--section",
    type=str,
    default=None,
    help="(Markdown) Read a specific section by heading text (case-insensitive)",
)
@click.option(
    "--block-type",
    type=click.Choice(["code", "table"]),
    default=None,
    help="(Markdown) Filter by block type within --section",
)
@add_output_options
@add_backend_options
@add_context_options
def cat(
    path: str,
    metadata: bool,
    at_operation: str | None,
    section: str | None,
    block_type: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
    operation_context: dict[str, Any],
) -> None:
    """Display file contents.

    Examples:
        nexus cat /workspace/data.txt
        nexus cat /workspace/code.py
        nexus cat /workspace/data.txt --metadata
        nexus cat /workspace/data.txt --json
        nexus cat /workspace/data.txt --at-operation op_abc123
        nexus cat /workspace/docs/arch.md --section Authentication
        nexus cat /workspace/docs/arch.md --section Auth --block-type code
        nexus cat /workspace/docs/arch.md#authentication
    """
    # Support fragment syntax: /path/file.md#section → path=/path/file.md, section=section
    if "#" in path and section is None:
        path, section = path.rsplit("#", 1)

    async def _impl() -> None:
        timing = CommandTiming()

        try:
            async with open_filesystem(
                remote_url,
                remote_api_key,
                allow_local_default=True,
            ) as nx:
                with timing.phase("connect"):
                    pass  # connection already established by async with

                if at_operation:
                    _cat_time_travel(nx, path, at_operation, metadata, output_opts, timing)
                    return

                with timing.phase("server"):
                    if metadata:
                        read_result = nx.read(
                            path, context=cast(Any, operation_context), return_metadata=True
                        )
                        assert isinstance(read_result, dict), (
                            "Expected dict when return_metadata=True"
                        )
                        content = read_result["content"]
                        meta_data = {
                            "path": path,
                            "content_id": read_result["content_id"],
                            "version": read_result["version"],
                            "size": read_result["size"],
                            "modified_at": str(read_result["modified_at"]),
                        }
                    else:
                        # Check file size to decide between read() and stream()
                        STREAM_THRESHOLD = 10 * 1024 * 1024  # 10MB
                        file_size = 0
                        if hasattr(nx, "metadata"):
                            try:
                                file_meta = nx.metadata.get(path)
                                file_size = file_meta.size if file_meta else 0
                            except Exception:
                                file_size = 0

                        if file_size > STREAM_THRESHOLD and not section:
                            console.print(
                                f"[nexus.muted]Streaming large file ({file_size:,} bytes)...[/nexus.muted]"
                            )
                            for chunk in nx.stream(
                                path, chunk_size=65536, context=cast(Any, operation_context)
                            ):
                                sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                            return

                        content = nx.sys_read(path, context=cast(Any, operation_context))
                        meta_data = None

                # --- Markdown partial read (Issue #3718) ---
                if section and path.endswith(".md"):
                    content_bytes = (
                        content if isinstance(content, bytes) else content.encode("utf-8")
                    )
                    partial = _cat_md_section(nx, path, content_bytes, section, block_type)
                    if partial is not None:
                        content = partial.encode("utf-8")

            # JSON mode: return structured data
            if output_opts.json_output:
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    text = None

                data: dict[str, Any] = {
                    "path": path,
                    "size": len(content),
                    "content": text,
                    "binary": text is None,
                }
                if meta_data:
                    data["metadata"] = meta_data

                render_output(data=data, output_opts=output_opts, timing=timing)
                return

            # Human mode
            if metadata and meta_data:
                console.print("[bold]Metadata:[/bold]")
                console.print(f"[nexus.muted]Path:[/nexus.muted]     {meta_data['path']}")
                console.print(f"[nexus.muted]Content-ID:[/nexus.muted] {meta_data['content_id']}")
                console.print(f"[nexus.muted]Version:[/nexus.muted]  {meta_data['version']}")
                console.print(f"[nexus.muted]Size:[/nexus.muted]     {meta_data['size']} bytes")
                console.print(f"[nexus.muted]Modified:[/nexus.muted] {meta_data['modified_at']}")
                console.print()
                console.print("[bold]Content:[/bold]")

            _print_content(path, content)

        except Exception as e:
            if output_opts.json_output:
                from nexus.cli.exit_codes import ExitCode

                render_error(
                    error=e,
                    output_opts=output_opts,
                    exit_code=ExitCode.GENERAL_ERROR,
                    timing=timing,
                )
            else:
                handle_error(e)

    asyncio.run(_impl())


def _cat_time_travel(
    nx: Any,
    path: str,
    at_operation: str,
    metadata: bool,
    output_opts: OutputOptions,
    timing: CommandTiming,
) -> None:
    """Handle time-travel cat (--at-operation)."""
    time_travel = getattr(nx, "time_travel_service", None)
    if time_travel is None:
        console.print(
            "[nexus.error]Error:[/nexus.error] Time-travel is only supported with local NexusFS"
        )
        return

    with timing.phase("server"):
        state = time_travel.get_file_at_operation(path, at_operation)

    content = state["content"]

    if output_opts.json_output:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = None

        data: dict[str, Any] = {
            "path": path,
            "operation_id": state["operation_id"],
            "operation_time": str(state["operation_time"]),
            "content": text,
            "binary": text is None,
            "metadata": state.get("metadata"),
        }
        render_output(data=data, output_opts=output_opts, timing=timing)
        return

    console.print("[bold nexus.value]Time-Travel Mode[/bold nexus.value]")
    console.print(f"[nexus.muted]Operation ID:[/nexus.muted]  {state['operation_id']}")
    console.print(f"[nexus.muted]Operation Time:[/nexus.muted] {state['operation_time']}")
    console.print()

    if metadata:
        console.print("[bold]Metadata:[/bold]")
        console.print(f"[nexus.muted]Path:[/nexus.muted]     {path}")
        console.print(
            f"[nexus.muted]Size:[/nexus.muted]     {state['metadata'].get('size', 0)} bytes"
        )
        console.print(f"[nexus.muted]Owner:[/nexus.muted]    {state['metadata'].get('owner', '-')}")
        console.print(f"[nexus.muted]Group:[/nexus.muted]    {state['metadata'].get('group', '-')}")
        console.print(f"[nexus.muted]Mode:[/nexus.muted]     {state['metadata'].get('mode', '-')}")
        console.print(
            f"[nexus.muted]Modified:[/nexus.muted] {state['metadata'].get('modified_at', '-')}"
        )
        console.print()
        console.print("[bold]Content:[/bold]")

    _print_content(path, content)


def _cat_md_section(
    nx: Any,
    path: str,
    content: bytes,
    section: str,
    block_type: str | None,
) -> str | None:
    """Attempt a partial markdown read using the structural index (Issue #3718)."""
    try:
        hook = nx.service("md_structure") if hasattr(nx, "service") else None
        if hook is None or not hasattr(hook, "read_section"):
            return None
        content_id = ""
        meta = getattr(nx, "metadata", None)
        if meta is not None:
            try:
                file_meta = meta.get(path)
                if file_meta and file_meta.content_id:
                    content_id = file_meta.content_id
            except Exception:
                pass
        result: str | None = hook.read_section(path, content, content_id, section, block_type)
        return result
    except Exception:
        return None


def _print_content(path: str, content: bytes) -> None:
    """Print file content with syntax highlighting where applicable."""
    try:
        text = content.decode("utf-8")
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
        console.print(f"[nexus.warning]Binary file ({len(content)} bytes)[/nexus.warning]")
        console.print(f"[nexus.muted]{content[:100]!r}...[/nexus.muted]")


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
    help="Show metadata (content_id, version) after writing",
)
@add_dry_run_option
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
    dry_run: bool,
    remote_url: str | None,
    remote_api_key: str | None,
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

        # Dry run (preview without writing)
        nexus write /workspace/data.txt "Hello" --dry-run
    """

    async def _impl() -> None:
        try:
            file_content = resolve_content(content, input_file)

            if dry_run:
                preview = dry_run_preview("write", path=path, details={"size": len(file_content)})
                render_dry_run(preview)
                return

            async with open_filesystem(
                remote_url,
                remote_api_key,
                allow_local_default=True,
            ) as nx:
                # OCC: use lib/occ helper if CAS params present (Issue #1323).
                ctx = cast(Any, operation_context)
                if (if_match or if_none_match) and not force:
                    from nexus.lib.occ import occ_write

                    result = await occ_write(
                        nx,
                        path,
                        file_content,
                        context=ctx,
                        if_match=if_match,
                        if_none_match=if_none_match,
                    )
                else:
                    result = nx.write(path, file_content, context=ctx)

            console.print(
                f"[nexus.success]✓[/nexus.success] Wrote {len(file_content)} bytes to [nexus.path]{path}[/nexus.path]"
            )

            if show_metadata:
                console.print(f"[nexus.muted]Content-ID:[/nexus.muted] {result['content_id']}")
                console.print(f"[nexus.muted]Version:[/nexus.muted]  {result['version']}")
                console.print(f"[nexus.muted]Size:[/nexus.muted]     {result['size']} bytes")
                console.print(f"[nexus.muted]Modified:[/nexus.muted] {result['modified_at']}")
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


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
    help="Show metadata (content_id, version) after appending",
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
    remote_url: str | None,
    remote_api_key: str | None,
    operation_context: dict[str, Any],
) -> None:
    """Append content to a file (creates file if it doesn't exist).

    This is useful for building log files, JSONL files, and other
    append-only data structures without reading the entire file first.

    Examples:
        nexus append /workspace/app.log "New log entry\\n"
        echo "New line" | nexus append /workspace/data.txt --input -
        nexus append /workspace/output.txt --input input.txt
        nexus append /doc.txt "New content" --if-match abc123...
        nexus append /log.txt "Entry\\n" --show-metadata
    """

    async def _impl() -> None:
        try:
            file_content = resolve_content(content, input_file)

            async with open_filesystem(
                remote_url,
                remote_api_key,
                allow_local_default=True,
            ) as nx:
                # Append with OCC parameters and context.
                # CAS params (if_match, force) are NexusFS-specific (transitional, see #1323).
                result = cast(Any, nx).append(
                    path,
                    file_content,
                    context=operation_context,
                    if_match=if_match,
                    force=force,
                )

            console.print(
                f"[nexus.success]✓[/nexus.success] Appended {len(file_content)} bytes to [nexus.path]{path}[/nexus.path]"
            )

            if show_metadata:
                console.print(f"[nexus.muted]Content-ID:[/nexus.muted] {result['content_id']}")
                console.print(f"[nexus.muted]Version:[/nexus.muted]  {result['version']}")
                console.print(f"[nexus.muted]Size:[/nexus.muted]     {result['size']} bytes")
                console.print(f"[nexus.muted]Modified:[/nexus.muted] {result['modified_at']}")
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


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
    remote_url: str | None,
    remote_api_key: str | None,
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

    async def _impl() -> None:
        try:
            import time

            from rich.progress import (
                BarColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TimeElapsedColumn,
            )

            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)
            source_path = Path(source_dir)

            # Ensure dest_prefix starts with /
            nonlocal dest_prefix
            if not dest_prefix.startswith("/"):
                dest_prefix = "/" + dest_prefix

            # Collect all files matching the pattern
            console.print(f"[nexus.path]Scanning[/nexus.path] {source_path} for files...")
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
                console.print("[nexus.warning]No files found matching criteria[/nexus.warning]")
                nx.close()
                return

            console.print(
                f"[nexus.value]Found {len(files_to_upload)} files to upload[/nexus.value]"
            )

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
            console.print("[nexus.success]✓ Batch upload complete![/nexus.success]")
            console.print(f"  Files uploaded:  [nexus.value]{total_files}[/nexus.value]")
            console.print(f"  Total size:      [nexus.value]{total_bytes:,}[/nexus.value] bytes")
            console.print(
                f"  Time elapsed:    [nexus.value]{elapsed_time:.2f}[/nexus.value] seconds"
            )
            if elapsed_time > 0:
                files_per_sec = total_files / elapsed_time
                mb_per_sec = (total_bytes / 1024 / 1024) / elapsed_time
                console.print(
                    f"  Throughput:      [nexus.value]{files_per_sec:.1f}[/nexus.value] files/sec"
                )
                console.print(
                    f"  Bandwidth:       [nexus.value]{mb_per_sec:.2f}[/nexus.value] MB/sec"
                )

        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command()
@click.argument("source", type=str)
@click.argument("dest", type=str)
@add_dry_run_option
@add_backend_options
def cp(
    source: str,
    dest: str,
    dry_run: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Copy a file (simple copy - for recursive copy use 'copy' command).

    Examples:
        nexus cp /workspace/source.txt /workspace/dest.txt
        nexus cp /workspace/source.txt /workspace/dest.txt --dry-run
    """

    async def _impl() -> None:
        try:
            if dry_run:
                preview = dry_run_preview("cp", path=source, details={"dest": dest})
                render_dry_run(preview)
                return

            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)

            # Use sys_copy for backend-native server-side copy when
            # available (S3 CopyObject, GCS rewrite), streaming for
            # cross-backend, or read+write as fallback.
            nx.sys_copy(source, dest)

            nx.close()

            console.print(
                f"[nexus.success]✓[/nexus.success] Copied [nexus.path]{source}[/nexus.path] → [nexus.path]{dest}[/nexus.path]"
            )
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


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
    remote_url: str | None,
    remote_api_key: str | None,
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

    async def _impl() -> None:
        try:
            from nexus.sync import copy_file, copy_recursive, is_local_path

            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)

            # Handle --no-checksum flag
            use_checksum = checksum and not no_checksum

            if recursive:
                # Use progress bar from sync module (tqdm)
                stats = await copy_recursive(nx, source, dest, checksum=use_checksum, progress=True)
                nx.close()

                # Display results
                console.print("[bold nexus.success]✓ Copy Complete![/bold nexus.success]")
                console.print(f"  Files checked: [nexus.value]{stats.files_checked}[/nexus.value]")
                console.print(
                    f"  Files copied: [nexus.success]{stats.files_copied}[/nexus.success]"
                )
                console.print(
                    f"  Files skipped: [nexus.warning]{stats.files_skipped}[/nexus.warning] (identical)"
                )
                console.print(
                    f"  Bytes transferred: [nexus.value]{stats.bytes_transferred:,}[/nexus.value]"
                )

                if stats.errors:
                    console.print(
                        f"\n[bold nexus.error]Errors:[/bold nexus.error] {len(stats.errors)}"
                    )
                    for error in stats.errors[:10]:  # Show first 10 errors
                        console.print(f"  [nexus.error]•[/nexus.error] {error}")

            else:
                # Single file copy
                is_source_local = is_local_path(source)
                is_dest_local = is_local_path(dest)

                bytes_copied = await copy_file(
                    nx, source, dest, is_source_local, is_dest_local, use_checksum
                )

                nx.close()

                if bytes_copied > 0:
                    console.print(
                        f"[nexus.success]✓[/nexus.success] Copied [nexus.path]{source}[/nexus.path] → [nexus.path]{dest}[/nexus.path] "
                        f"({bytes_copied:,} bytes)"
                    )
                else:
                    console.print(
                        f"[nexus.warning]⊘[/nexus.warning] Skipped [nexus.path]{source}[/nexus.path] (identical content)"
                    )

        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command(name="move")
@click.argument("source", type=str)
@click.argument("dest", type=str)
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@add_dry_run_option
@add_backend_options
def move_cmd(
    source: str,
    dest: str,
    force: bool,
    dry_run: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Move files or directories.

    Move files from source to destination. This is an efficient rename
    when possible, otherwise copy + delete.

    Examples:
        nexus move /workspace/old.txt /workspace/new.txt
        nexus move /workspace/old_dir/ /workspace/new_dir/ --force
        nexus move /workspace/old.txt /workspace/new.txt --dry-run
    """

    async def _impl() -> None:
        try:
            if dry_run:
                preview = dry_run_preview("move", path=source, details={"dest": dest})
                render_dry_run(preview)
                return

            from nexus.sync import move_file

            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)

            # Confirm unless --force
            if not force and not click.confirm(f"Move {source} to {dest}?"):
                console.print("[nexus.warning]Cancelled[/nexus.warning]")
                nx.close()
                return

            with console.status(
                f"[nexus.warning]Moving {source} to {dest}...[/nexus.warning]", spinner="dots"
            ):
                success = await move_file(nx, source, dest, force=force)

            nx.close()

            if success:
                console.print(
                    f"[nexus.success]✓[/nexus.success] Moved [nexus.path]{source}[/nexus.path] → [nexus.path]{dest}[/nexus.path]"
                )
            else:
                console.print(f"[nexus.error]Error:[/nexus.error] Failed to move {source}")
                sys.exit(1)

        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


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
    remote_url: str | None,
    remote_api_key: str | None,
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

    async def _impl() -> None:
        try:
            from nexus.sync import sync_directories

            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)

            use_checksum = not no_checksum

            # Display sync configuration
            console.print(f"[nexus.path]Syncing:[/nexus.path] {source} → {dest}")
            if delete:
                console.print("  [nexus.warning]⚠ Delete mode enabled[/nexus.warning]")
            if dry_run:
                console.print("  [nexus.warning]DRY RUN - No changes will be made[/nexus.warning]")
            if not use_checksum:
                console.print(
                    "  [nexus.warning]Checksum disabled - copying all files[/nexus.warning]"
                )
            console.print()

            # Use progress bar from sync module (tqdm)
            stats = await sync_directories(
                nx,
                source,
                dest,
                delete=delete,
                dry_run=dry_run,
                checksum=use_checksum,
                progress=True,
            )

            nx.close()

            # Display results
            if dry_run:
                console.print("[bold nexus.warning]DRY RUN RESULTS:[/bold nexus.warning]")
            else:
                console.print("[bold nexus.success]✓ Sync Complete![/bold nexus.success]")

            console.print(f"  Files checked: [nexus.value]{stats.files_checked}[/nexus.value]")
            console.print(f"  Files copied: [nexus.success]{stats.files_copied}[/nexus.success]")
            console.print(
                f"  Files skipped: [nexus.warning]{stats.files_skipped}[/nexus.warning] (identical)"
            )

            if delete:
                console.print(f"  Files deleted: [nexus.error]{stats.files_deleted}[/nexus.error]")

            if not dry_run:
                console.print(
                    f"  Bytes transferred: [nexus.value]{stats.bytes_transferred:,}[/nexus.value]"
                )

            if stats.errors:
                console.print(f"\n[bold nexus.error]Errors:[/bold nexus.error] {len(stats.errors)}")
                for error in stats.errors[:10]:  # Show first 10 errors
                    console.print(f"  [nexus.error]•[/nexus.error] {error}")

        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command()
@click.argument("path", type=str)
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@add_dry_run_option
@add_backend_options
def rm(
    path: str,
    force: bool,
    dry_run: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Delete a file.

    Examples:
        nexus rm /workspace/data.txt
        nexus rm /workspace/data.txt --force
        nexus rm /workspace/data.txt --dry-run
    """

    async def _impl() -> None:
        try:
            if dry_run:
                preview = dry_run_preview("rm", path=path)
                render_dry_run(preview)
                return

            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)

            # Check if file exists
            if not nx.access(path):
                console.print(f"[nexus.warning]File does not exist:[/nexus.warning] {path}")
                nx.close()
                return

            # Confirm deletion unless --force
            if not force and not click.confirm(f"Delete {path}?"):
                console.print("[nexus.warning]Cancelled[/nexus.warning]")
                nx.close()
                return

            nx.sys_unlink(path)
            nx.close()

            console.print(
                f"[nexus.success]✓[/nexus.success] Deleted [nexus.path]{path}[/nexus.path]"
            )
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command()
@click.argument("path", type=str)
@click.argument("old_string", type=str)
@click.argument("new_string", type=str)
@click.option(
    "--fuzzy",
    type=float,
    default=None,
    help="Fuzzy matching threshold (0.0-1.0). Default: exact match only.",
)
@click.option(
    "--if-match",
    type=str,
    default=None,
    help="Only edit if current ETag matches (optimistic concurrency control).",
)
@click.option(
    "--preview",
    is_flag=True,
    help="Show what would change without writing.",
)
@add_backend_options
def edit(
    path: str,
    old_string: str,
    new_string: str,
    fuzzy: float | None,
    if_match: str | None,
    preview: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Surgical search-and-replace edit on a file.

    Finds OLD_STRING in the file and replaces it with NEW_STRING.
    Creates a new version on success. Supports exact, whitespace-normalized,
    and fuzzy matching.

    Examples:
        # Exact replacement
        nexus edit /workspace/doc.md "old text" "new text"

        # Fuzzy match (tolerates typos, threshold 0.0-1.0)
        nexus edit /workspace/doc.md "aproximate" "approximate" --fuzzy 0.8

        # Preview changes without writing
        nexus edit /workspace/doc.md "old" "new" --preview

        # OCC: only edit if no one else modified the file
        nexus edit /workspace/doc.md "old" "new" --if-match abc123
    """

    async def _impl() -> None:
        try:
            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)

            kwargs: dict[str, Any] = {
                "edits": [(old_string, new_string)],
                "preview": preview,
            }
            if fuzzy is not None:
                kwargs["fuzzy_threshold"] = fuzzy
            if if_match is not None:
                kwargs["if_match"] = if_match

            result = nx.edit(path, **kwargs)
            nx.close()

            success = result.get("success", False)
            applied = result.get("applied_count", 0)
            diff = result.get("diff", "")
            matches = result.get("matches", [])
            content_id = result.get("content_id", "")

            if preview:
                if success and applied > 0:
                    console.print(
                        f"[nexus.warning]Preview:[/nexus.warning] {applied} edit(s) would apply"
                    )
                    if diff:
                        syntax = Syntax(diff, "diff", theme="monokai")
                        console.print(syntax)
                else:
                    console.print("[nexus.warning]Preview:[/nexus.warning] No matches found")
                return

            if success and applied > 0:
                console.print(
                    f"[nexus.success]✓[/nexus.success] {applied} edit(s) applied to [nexus.path]{path}[/nexus.path]"
                )
                if content_id:
                    console.print(f"  [nexus.muted]content_id: {content_id}[/nexus.muted]")
                for m in matches:
                    sim = m.get("similarity")
                    method = m.get("method", "exact")
                    if sim and sim < 1.0:
                        console.print(
                            f"  [nexus.muted]match: similarity={sim:.2f} ({method})[/nexus.muted]"
                        )
                if diff:
                    syntax = Syntax(diff, "diff", theme="monokai")
                    console.print(syntax)
            else:
                console.print(
                    f"[nexus.warning]No matches found[/nexus.warning] for the search string in {path}"
                )
                if fuzzy is None:
                    console.print(
                        "[nexus.muted]Hint: use --fuzzy 0.8 for approximate matching[/nexus.muted]"
                    )
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())
