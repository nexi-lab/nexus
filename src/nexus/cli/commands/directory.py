"""Directory operation commands - ls, mkdir, rmdir, tree."""

import asyncio
import contextlib
from typing import Any

import click

from nexus.cli.dry_run import add_dry_run_option, dry_run_preview, render_dry_run
from nexus.cli.formatters import format_timestamp
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    add_backend_options,
    console,
    handle_error,
    open_filesystem,
)


def register_commands(cli: click.Group) -> None:
    """Register all directory operation commands."""
    cli.add_command(list_files)
    cli.add_command(mkdir)
    cli.add_command(rmdir)
    cli.add_command(tree)


def _normalize_readdir(raw: Any) -> list[Any]:
    """Unwrap sys_readdir result — remote mode returns ``{"files": [...]}`` envelope."""
    if isinstance(raw, dict) and "files" in raw:
        return list(raw["files"])
    if isinstance(raw, list):
        return raw
    return []


def _format_file_entry(file: dict[str, Any] | str) -> dict[str, Any]:
    """Normalize a file entry dict for consistent JSON output."""
    if isinstance(file, str):
        is_dir = file.endswith("/")
        return {
            "path": file.rstrip("/"),
            "type": "directory" if is_dir else "file",
            "size": None if is_dir else 0,
            "modified_at": None,
        }
    is_dir = file.get("is_directory", False)
    return {
        "path": file["path"],
        "type": "directory" if is_dir else "file",
        "size": file.get("size", 0) if not is_dir else None,
        "modified_at": file.get("modified_at"),
    }


@click.command(name="ls")
@click.argument("path", default="/", type=str)
@click.option("-r", "--recursive", is_flag=True, help="List files recursively")
@click.option("-l", "--long", is_flag=True, help="Show detailed information")
@click.option(
    "--at-operation",
    type=str,
    help="List files at a historical operation point (time-travel debugging)",
)
@add_output_options
@add_backend_options
def list_files(
    path: str,
    recursive: bool,
    long: bool,
    at_operation: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List files in a directory.

    Examples:
        nexus ls /workspace
        nexus ls /workspace --recursive
        nexus ls /workspace -l
        nexus ls /workspace --json
        nexus ls /workspace --json --fields path,size

    \b
        # Time-travel: List files at historical operation point
        nexus ls /workspace --at-operation op_abc123
    """

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
                    _ls_time_travel(nx, path, at_operation, recursive, long, output_opts, timing)
                    return

                with timing.phase("server"):
                    files_raw = nx.sys_readdir(path, recursive=recursive, details=True)
                    files = _normalize_readdir(files_raw)

            if not files:
                render_output(
                    data=[],
                    output_opts=output_opts,
                    timing=timing,
                    message=f"No files found in {path}",
                )
                return

            data = [_format_file_entry(f) for f in files]

            def _print_human(entries: list[dict[str, Any]]) -> None:
                if long:
                    from rich.table import Table

                    table = Table(title=f"Files in {path}")
                    table.add_column("Type", style="nexus.identity", width=4)
                    table.add_column("Path", style="nexus.path")
                    table.add_column("Size", justify="right", style="nexus.success")
                    table.add_column("Modified", style="nexus.warning")

                    for entry in entries:
                        is_dir = entry["type"] == "directory"
                        table.add_row(
                            "dir" if is_dir else "file",
                            f"{entry['path']}/" if is_dir else entry["path"],
                            f"{entry['size']:,} bytes" if entry["size"] is not None else "-",
                            format_timestamp(entry.get("modified_at"))
                            if entry.get("modified_at")
                            else "-",
                        )
                    console.print(table)
                else:
                    for entry in entries:
                        if entry["type"] == "directory":
                            console.print(
                                f"  [bold nexus.value]{entry['path']}/[/bold nexus.value]"
                            )
                        else:
                            console.print(f"  {entry['path']}")

            render_output(
                data=data,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_print_human,
            )
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


def _ls_time_travel(
    nx: Any,
    path: str,
    at_operation: str,
    recursive: bool,
    long: bool,  # noqa: ARG001
    output_opts: OutputOptions,
    timing: CommandTiming,
) -> None:
    """Handle time-travel ls (--at-operation)."""
    time_travel = getattr(nx, "time_travel_service", None)
    if time_travel is None:
        console.print(
            "[nexus.error]Error:[/nexus.error] Time-travel is only supported with local NexusFS"
        )
        return

    with timing.phase("server"):
        files = time_travel.list_files_at_operation(path, at_operation, recursive=recursive)

    if not files:
        render_output(
            data=[],
            output_opts=output_opts,
            timing=timing,
            message=f"No files found in {path} at operation {at_operation}",
        )
        return

    data = [
        {
            "path": f["path"],
            "size": f.get("size", 0),
            "owner": f.get("owner"),
            "modified_at": f.get("modified_at"),
        }
        for f in files
    ]

    def _print_human(entries: list[dict[str, Any]]) -> None:
        console.print(
            f"[bold nexus.value]Time-Travel Mode - Files at operation {at_operation}[/bold nexus.value]"
        )
        console.print()
        for entry in entries:
            console.print(f"  {entry['path']}")

    render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_print_human)


@click.command()
@click.argument("path", type=str)
@click.option("-p", "--parents", is_flag=True, help="Create parent directories as needed")
@click.option(
    "--if-not-exists",
    is_flag=True,
    default=False,
    help="Succeed silently if directory exists, returning existing directory info",
)
@add_dry_run_option
@add_backend_options
def mkdir(
    path: str,
    parents: bool,
    if_not_exists: bool,
    dry_run: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a directory.

    Examples:
        nexus mkdir /workspace/data
        nexus mkdir /workspace/deep/nested/dir --parents
        nexus mkdir /workspace/data --dry-run
        nexus mkdir /workspace/data --if-not-exists
    """

    async def _impl() -> None:
        try:
            if dry_run:
                preview = dry_run_preview("mkdir", path=path, details={"parents": parents})
                render_dry_run(preview)
                return

            async with open_filesystem(
                remote_url,
                remote_api_key,
                allow_local_default=True,
            ) as nx:
                if if_not_exists:
                    with contextlib.suppress(FileExistsError):
                        nx.mkdir(path, parents=parents, exist_ok=True)
                    console.print(
                        f"[nexus.success]✓[/nexus.success] Directory exists: [nexus.path]{path}[/nexus.path]"
                    )
                else:
                    nx.mkdir(path, parents=parents, exist_ok=True)
                    console.print(
                        f"[nexus.success]✓[/nexus.success] Created directory [nexus.path]{path}[/nexus.path]"
                    )
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command()
@click.argument("path", type=str)
@click.option("-r", "--recursive", is_flag=True, help="Remove directory and contents")
@click.option("-f", "--force", is_flag=True, help="Don't ask for confirmation")
@add_dry_run_option
@add_backend_options
def rmdir(
    path: str,
    recursive: bool,
    force: bool,
    dry_run: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Remove a directory.

    Examples:
        nexus rmdir /workspace/data
        nexus rmdir /workspace/data --recursive --force
        nexus rmdir /workspace/data --dry-run
    """

    async def _impl() -> None:
        try:
            if dry_run:
                preview = dry_run_preview(
                    "rmdir",
                    path=path,
                    details={"recursive": recursive},
                )
                render_dry_run(preview)
                return

            async with open_filesystem(
                remote_url,
                remote_api_key,
                allow_local_default=True,
            ) as nx:
                if not force and not click.confirm(f"Remove directory {path}?"):
                    console.print("[nexus.warning]Cancelled[/nexus.warning]")
                    return
                nx.rmdir(path, recursive=recursive)
            console.print(
                f"[nexus.success]✓[/nexus.success] Removed directory [nexus.path]{path}[/nexus.path]"
            )
        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())


@click.command(name="tree")
@click.argument("path", default="/", type=str)
@click.option("-L", "--level", type=int, default=None, help="Max depth to display")
@click.option("--show-size", is_flag=True, help="Show file sizes")
@add_output_options
@add_backend_options
def tree(
    path: str,
    level: int | None,
    show_size: bool,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Display directory tree structure.

    Examples:
        nexus tree /workspace
        nexus tree /workspace -L 2
        nexus tree /workspace --show-size
        nexus tree /workspace --json
    """

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

                with timing.phase("server"):
                    files_raw = nx.sys_readdir(path, recursive=True, details=True)

            files = _normalize_readdir(files_raw)
            if not files:
                render_output(
                    data={"files": [], "total_files": 0},
                    output_opts=output_opts,
                    timing=timing,
                    message=f"No files found in {path}",
                )
                return

            entries = [_format_file_entry(f) for f in files]

            # Apply level filter for JSON
            if level is not None:
                entries = [e for e in entries if e["path"].count("/") <= level]

            tree_data = {
                "root": path,
                "files": entries,
                "total_files": len(entries),
                "total_size": sum(e["size"] or 0 for e in entries),
            }

            def _print_human(data: dict[str, Any]) -> None:
                from collections import defaultdict
                from pathlib import PurePosixPath

                tree_dict: dict[str, Any] = defaultdict(dict)
                # Strip the base path prefix so depth is relative to target path
                base_parts = PurePosixPath(path).parts
                for entry in data["files"]:
                    parts = PurePosixPath(entry["path"]).parts
                    rel_parts = parts[len(base_parts) :]
                    if not rel_parts:
                        continue
                    current = tree_dict
                    for i, part in enumerate(rel_parts):
                        if i == len(rel_parts) - 1:
                            current[part] = entry["size"]
                        else:
                            if part not in current or not isinstance(current[part], dict):
                                current[part] = {}
                            current = current[part]

                def _fmt_size(size: int) -> str:
                    size_float = float(size)
                    for unit in ["B", "KB", "MB", "GB", "TB"]:
                        if size_float < 1024.0:
                            return f"{size_float:.1f} {unit}"
                        size_float /= 1024.0
                    return f"{size_float:.1f} PB"

                def _print_node(
                    node: dict[str, Any], prefix: str = "", depth: int = 0
                ) -> tuple[int, int]:
                    if level is not None and depth >= level:
                        return 0, 0
                    items = sorted(node.items())
                    total_files = 0
                    total_size = 0
                    for i, (name, value) in enumerate(items):
                        is_last = i == len(items) - 1
                        connector = "└── " if is_last else "├── "
                        extension = "    " if is_last else "│   "
                        if isinstance(value, dict):
                            console.print(
                                f"{prefix}{connector}[bold nexus.value]{name}/[/bold nexus.value]"
                            )
                            f, s = _print_node(value, prefix + extension, depth + 1)
                            total_files += f
                            total_size += s
                        else:
                            total_files += 1
                            if show_size and value is not None:
                                console.print(
                                    f"{prefix}{connector}{name} [nexus.muted]({_fmt_size(value)})[/nexus.muted]"
                                )
                                total_size += value
                            else:
                                console.print(f"{prefix}{connector}{name}")
                    return total_files, total_size

                console.print(f"[bold nexus.success]{path}[/bold nexus.success]")
                file_count, total_sz = _print_node(tree_dict)
                console.print()
                if show_size:
                    console.print(
                        f"[nexus.muted]{file_count} files, {_fmt_size(total_sz)} total[/nexus.muted]"
                    )
                else:
                    console.print(f"[nexus.muted]{file_count} files[/nexus.muted]")

            render_output(
                data=tree_data, output_opts=output_opts, timing=timing, human_formatter=_print_human
            )
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
