"""Unified CLI output infrastructure.

Provides ``add_output_options`` decorator and ``render_output`` function so that
every CLI command can support ``--json``, ``--quiet``, ``-v``/``-vv``/``-vvv``,
and ``--fields`` with consistent behavior.

TTY auto-detection: when stdout is not a terminal (piped), output automatically
switches to JSON (like ``gh`` CLI).

Usage::

    @click.command()
    @add_output_options
    async def ls(output_opts: OutputOptions, ...) -> None:
        timing = CommandTiming()
        with timing.phase("server"):
            files = nx.sys_readdir(path)

        render_output(
            data=files,
            output_opts=output_opts,
            timing=timing,
            human_formatter=lambda d: _print_file_table(d),
        )
"""

from __future__ import annotations

import functools
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import click

from nexus.cli.exit_codes import ExitCode
from nexus.cli.timing import CommandTiming, timing_enabled


@dataclass(frozen=True)
class OutputOptions:
    """Immutable bag of output-related CLI flags."""

    json_output: bool
    quiet: bool
    verbosity: int  # 0=default, 1=-v, 2=-vv, 3=-vvv
    fields: str | None
    request_id: str
    # Distinguishes ``--json`` passed explicitly from the auto-JSON
    # fallback that kicks in when stdout is piped. Commands that have
    # their own pipe-friendly plain output format (``nexus grep -l``,
    # ``nexus glob --plain``) check this flag so they can bypass the
    # auto-JSON fallback without clobbering users who really wanted
    # JSON (#3701).
    json_output_explicit: bool = False


def _auto_json() -> bool:
    """Return True when stdout is not a TTY (piped) and NO_COLOR is not set."""
    if os.environ.get("NEXUS_NO_AUTO_JSON", ""):
        return False
    return not sys.stdout.isatty()


def add_output_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator adding --json, --quiet, -v/--verbose, --fields to a command.

    Injects an ``output_opts: OutputOptions`` keyword argument.
    """

    @click.option("--json", "json_output", is_flag=True, default=False, help="Output as JSON")
    @click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress non-error output")
    @click.option(
        "--verbose",
        "-v",
        "verbosity",
        count=True,
        help="Increase verbosity (-v timing, -vv debug, -vvv trace)",
    )
    @click.option(
        "--fields",
        type=str,
        default=None,
        help="Comma-separated fields to include in JSON output",
    )
    @functools.wraps(func)
    def wrapper(
        json_output: bool,
        quiet: bool,
        verbosity: int,
        fields: str | None,
        **kwargs: Any,
    ) -> Any:
        # Remember whether the user passed --json explicitly BEFORE the
        # auto-JSON fallback kicks in. Commands that have a pipe-friendly
        # plain output format use this to bypass auto-JSON without
        # clobbering users who explicitly asked for JSON (#3701).
        json_output_explicit = json_output
        # Auto-JSON when piped
        if not json_output and _auto_json():
            json_output = True

        request_id = uuid.uuid4().hex

        output_opts = OutputOptions(
            json_output=json_output,
            quiet=quiet,
            verbosity=verbosity,
            fields=fields,
            request_id=request_id,
            json_output_explicit=json_output_explicit,
        )
        return func(output_opts=output_opts, **kwargs)

    return wrapper


def _filter_fields(data: Any, fields: str) -> Any:
    """Filter dict/list-of-dicts to only include specified fields."""
    field_set = {f.strip() for f in fields.split(",")}
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in field_set}
    if isinstance(data, list):
        return [
            {k: v for k, v in item.items() if k in field_set}
            for item in data
            if isinstance(item, dict)
        ]
    return data


def render_output(
    *,
    data: Any,
    output_opts: OutputOptions,
    timing: CommandTiming | None = None,
    human_formatter: Callable[[Any], None] | None = None,
    message: str | None = None,
) -> None:
    """Render command output respecting output_opts.

    Args:
        data: The structured data to output (dict, list, or scalar).
        output_opts: Output options from the decorator.
        timing: Optional timing information.
        human_formatter: Callable that prints human-readable output. If None,
            ``message`` is printed instead.
        message: Simple message for human output when no formatter is provided.
    """
    if output_opts.quiet and not output_opts.json_output:
        return

    if output_opts.json_output:
        _render_json(data=data, output_opts=output_opts, timing=timing)
    else:
        _render_human(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=human_formatter,
            message=message,
        )


def _render_json(
    *,
    data: Any,
    output_opts: OutputOptions,
    timing: CommandTiming | None,
) -> None:
    """Render JSON to stdout."""
    # Apply field filtering
    if output_opts.fields and data is not None:
        data = _filter_fields(data, output_opts.fields)

    envelope: dict[str, Any] = {"data": data}

    if timing is not None:
        envelope["_timing"] = timing.to_dict()

    if output_opts.verbosity >= 3:
        envelope["_request_id"] = output_opts.request_id

    click.echo(json.dumps(envelope, indent=2, default=str))


def _render_human(
    *,
    data: Any,
    output_opts: OutputOptions,
    timing: CommandTiming | None,
    human_formatter: Callable[[Any], None] | None,
    message: str | None,
) -> None:
    """Render human-readable output with optional timing on stderr."""
    if human_formatter is not None:
        human_formatter(data)
    elif message is not None:
        click.echo(message)

    # Timing on stderr
    if timing is not None and timing_enabled(output_opts.verbosity):
        if output_opts.verbosity >= 3:
            click.echo(f"request_id: {output_opts.request_id}", err=True)
            click.echo(timing.format_breakdown(), err=True)
        else:
            click.echo(timing.format_short(), err=True)


def _api_error_to_exit_code(error: Exception) -> ExitCode | None:
    """Map NexusAPIError HTTP status to ExitCode, or None if not applicable."""
    from nexus.cli.clients.base import NexusAPIError

    if not isinstance(error, NexusAPIError):
        return None

    status = error.status_code
    if status == 400:
        return ExitCode.USAGE_ERROR
    if status in {401, 403}:
        return ExitCode.PERMISSION_DENIED
    if status == 404:
        return ExitCode.NOT_FOUND
    if status in {408, 504}:
        return ExitCode.TEMPFAIL
    if status in {429, 503}:
        return ExitCode.UNAVAILABLE
    if status >= 500:
        return ExitCode.INTERNAL_ERROR
    return ExitCode.GENERAL_ERROR


def render_error(
    *,
    error: Exception,
    output_opts: OutputOptions | None = None,
    exit_code: ExitCode = ExitCode.GENERAL_ERROR,
    timing: CommandTiming | None = None,
) -> None:
    """Render an error and exit with the appropriate code.

    In JSON mode, outputs a structured error object. In human mode, prints
    a Rich-formatted error message to stderr.
    """
    error_code = _exception_to_error_code(error)

    # Override exit_code when the error is a NexusAPIError
    api_exit = _api_error_to_exit_code(error)
    if api_exit is not None:
        exit_code = api_exit

    if output_opts is not None and output_opts.json_output:
        envelope: dict[str, Any] = {
            "data": None,
            "error": {
                "code": error_code,
                "message": str(error),
                "type": type(error).__name__,
            },
        }
        if timing is not None:
            envelope["_timing"] = timing.to_dict()
        if output_opts.verbosity >= 3:
            envelope["_request_id"] = output_opts.request_id
        click.echo(json.dumps(envelope, indent=2, default=str))
    else:
        from nexus.cli.theme import err_console

        err_console.print(f"[nexus.error]Error:[/nexus.error] {error}")

    sys.exit(exit_code)


def _exception_to_error_code(error: Exception) -> str:
    """Map an exception to a short machine-readable error code."""
    # NexusAPIError — map HTTP status codes to error codes
    from nexus.cli.clients.base import NexusAPIError

    if isinstance(error, NexusAPIError):
        status = error.status_code
        if status == 400:
            return "VALIDATION_ERROR"
        if status in {401, 403}:
            return "PERMISSION_DENIED"
        if status == 404:
            return "NOT_FOUND"
        if status in {408, 504}:
            return "TIMEOUT"
        if status in {429, 503}:
            return "UNAVAILABLE"
        if status >= 500:
            return "INTERNAL_ERROR"
        return "INTERNAL_ERROR"

    # Lazy import to avoid circular deps
    from nexus.contracts.exceptions import (
        AccessDeniedError,
        NexusFileNotFoundError,
        NexusPermissionError,
        ValidationError,
    )

    if isinstance(error, NexusFileNotFoundError):
        return "NOT_FOUND"
    if isinstance(error, PermissionError | AccessDeniedError | NexusPermissionError):
        return "PERMISSION_DENIED"
    if isinstance(error, ValidationError):
        return "VALIDATION_ERROR"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, ConnectionError | OSError):
        return "CONNECTION_ERROR"
    return "INTERNAL_ERROR"
