"""Nexus CLI Inspect Commands - File inspection and system info.

Commands for viewing file information, version, and calculating sizes.
"""

import sys
from typing import Any, cast

import click
from rich.table import Table

import nexus
from nexus.cli.formatters import format_timestamp
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
    open_filesystem,
)


@click.command()
@click.argument("path", type=str)
@add_output_options
@add_backend_options
def info(
    path: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show detailed file information.

    Examples:
        nexus info /workspace/data.txt
        nexus info /workspace/data.txt --json
        nexus info /workspace/data.txt --json --fields path,size,etag
    """
    import asyncio

    asyncio.run(_async_info(path, output_opts, remote_url, remote_api_key))


async def _async_info(
    path: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    timing = CommandTiming()

    try:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            with timing.phase("connect"):
                pass  # connection already established by async with

            # Check if file exists first
            if not nx.access(path):
                render_output(
                    data=None,
                    output_opts=output_opts,
                    timing=timing,
                    message=f"File not found: {path}",
                )
                sys.exit(1)

            with timing.phase("server"):
                file_meta = cast(Any, nx).metadata.get(path)

        if not file_meta:
            render_output(
                data=None, output_opts=output_opts, timing=timing, message=f"File not found: {path}"
            )
            sys.exit(1)

        created_str = format_timestamp(file_meta.created_at)
        modified_str = format_timestamp(file_meta.modified_at)

        data = {
            "path": file_meta.path,
            "size": file_meta.size,
            "created_at": created_str,
            "modified_at": modified_str,
            "etag": file_meta.etag or None,
            "mime_type": file_meta.mime_type or None,
        }

        def _print_human(d: dict[str, Any]) -> None:
            table = Table(title=f"File Information: {path}")
            table.add_column("Property", style="nexus.value")
            table.add_column("Value", style="nexus.success")
            table.add_row("Path", d["path"])
            table.add_row("Size", f"{d['size']:,} bytes")
            table.add_row("Created", d["created_at"])
            table.add_row("Modified", d["modified_at"])
            table.add_row("ETag", d["etag"] or "N/A")
            table.add_row("MIME Type", d["mime_type"] or "N/A")
            console.print(table)

        render_output(
            data=data, output_opts=output_opts, timing=timing, human_formatter=_print_human
        )
    except Exception as e:
        if output_opts.json_output:
            from nexus.cli.exit_codes import ExitCode

            render_error(
                error=e, output_opts=output_opts, exit_code=ExitCode.GENERAL_ERROR, timing=timing
            )
        else:
            handle_error(e)


@click.command()
def version() -> None:
    """Show Nexus version information."""
    console.print(
        f"[nexus.value]Nexus[/nexus.value] version [nexus.success]{nexus.__version__}[/nexus.success]"
    )


@click.command(name="size")
@click.argument("path", default="/", type=str)
@click.option("--human", "-h", is_flag=True, help="Human-readable output")
@click.option("--details", is_flag=True, help="Show per-file breakdown")
@add_backend_options
def size(
    path: str,
    human: bool,
    details: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Calculate total size of files in a path.

    Recursively calculates the total size of all files under a given path.

    Examples:
        nexus size /workspace
        nexus size /workspace --human
        nexus size /workspace --details
    """
    import asyncio

    asyncio.run(_async_size(path, human, details, remote_url, remote_api_key))


async def _async_size(
    path: str,
    human: bool,
    details: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        # Get all files with details
        with console.status(
            f"[nexus.warning]Calculating size of {path}...[/nexus.warning]", spinner="dots"
        ):
            files_raw = nx.sys_readdir(path, recursive=True, details=True)

        nx.close()

        if not files_raw:
            console.print(f"[nexus.warning]No files found in {path}[/nexus.warning]")
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
        console.print(f"[bold nexus.value]Size of {path}:[/bold nexus.value]")
        console.print(f"  Total size: [nexus.success]{format_size(total_size)}[/nexus.success]")
        console.print(f"  File count: [nexus.value]{file_count:,}[/nexus.value]")

        if details:
            console.print()
            console.print("[bold]Top 10 largest files:[/bold]")

            # Sort by size and show top 10
            sorted_files = sorted(files, key=lambda f: f["size"], reverse=True)[:10]

            table = Table()
            table.add_column("Size", justify="right", style="nexus.success")
            table.add_column("Path", style="nexus.path")

            for file in sorted_files:
                table.add_row(format_size(file["size"]), file["path"])

            console.print(table)

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register all metadata commands to the CLI group.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(info)
    cli.add_command(version)
    cli.add_command(size)
