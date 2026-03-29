"""Dry-run infrastructure for CLI commands.

Provides ``add_dry_run_option`` decorator that adds ``--dry-run`` to mutating
commands and injects a ``dry_run: bool`` kwarg. Also provides helpers for
generating structured dry-run preview output.

Usage::

    @click.command()
    @add_dry_run_option
    @add_output_options
    def write(dry_run: bool, output_opts: OutputOptions, ...) -> None:
        if dry_run:
            preview = dry_run_preview("write", path=path, bytes=len(content))
            render_dry_run(preview, output_opts)
            return
        # ... actual write logic
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import click


def add_dry_run_option(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator adding ``--dry-run`` flag to a mutating command.

    Injects a ``dry_run: bool`` keyword argument.
    """

    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Preview changes without making them",
    )
    @functools.wraps(func)
    def wrapper(dry_run: bool, **kwargs: Any) -> Any:
        return func(dry_run=dry_run, **kwargs)

    return wrapper


def dry_run_preview(
    operation: str,
    *,
    path: str | None = None,
    source: str | None = None,
    dest: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured dry-run preview dict.

    Args:
        operation: The operation name (write, rm, mkdir, etc.)
        path: Target path for single-target operations
        source: Source path for copy/move operations
        dest: Destination path for copy/move operations
        details: Additional operation-specific details

    Returns:
        Structured preview dict suitable for JSON or human rendering
    """
    preview: dict[str, Any] = {
        "dry_run": True,
        "operation": operation,
    }
    if path is not None:
        preview["path"] = path
    if source is not None:
        preview["source"] = source
    if dest is not None:
        preview["dest"] = dest
    if details:
        preview.update(details)
    return preview


def render_dry_run(
    preview: dict[str, Any],
    output_opts: Any | None = None,
) -> None:
    """Render a dry-run preview to the console.

    In JSON mode: outputs the preview dict via render_output().
    In human mode: prints a formatted preview summary.

    Args:
        preview: Structured preview from dry_run_preview()
        output_opts: OutputOptions (if available). When None, uses human mode.
    """
    if output_opts is not None and output_opts.json_output:
        from nexus.cli.output import render_output

        render_output(data=preview, output_opts=output_opts, message="Dry run preview")
        return

    # Human mode: formatted preview
    from nexus.cli.theme import console

    op = preview.get("operation", "unknown")
    path = preview.get("path", "")
    source = preview.get("source", "")
    dest = preview.get("dest", "")

    console.print("[bold nexus.warning]DRY RUN[/bold nexus.warning] — no changes will be made")

    if source and dest:
        console.print(
            f"  Would {op}: [nexus.path]{source}[/nexus.path] → [nexus.path]{dest}[/nexus.path]"
        )
    elif path:
        console.print(f"  Would {op}: [nexus.path]{path}[/nexus.path]")

    # Print any additional details
    skip_keys = {"dry_run", "operation", "path", "source", "dest"}
    for key, value in preview.items():
        if key not in skip_keys:
            console.print(f"  {key}: {value}")
