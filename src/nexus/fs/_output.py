"""Standalone CLI output infrastructure for nexus-fs.

Mirrors the ``nexus.cli.output`` interface so that nexus-fs commands get
``--json``, ``--quiet``, ``-v``/``--verbose``, and ``--fields`` — without
importing from ``nexus.cli`` (which is excluded from the slim wheel).

The JSON envelope format is intentionally identical to the main CLI::

    {"data": <payload>, "_request_id": "..."}
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


@dataclass(frozen=True)
class OutputOptions:
    """Immutable bag of output-related CLI flags."""

    json_output: bool
    quiet: bool
    verbosity: int  # 0=default, 1=-v, 2=-vv, 3=-vvv
    fields: str | None
    request_id: str


def _auto_json() -> bool:
    """Return True when stdout is not a TTY (piped)."""
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
        if not json_output and _auto_json():
            json_output = True

        request_id = uuid.uuid4().hex

        output_opts = OutputOptions(
            json_output=json_output,
            quiet=quiet,
            verbosity=verbosity,
            fields=fields,
            request_id=request_id,
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
    human_formatter: Callable[[Any], None] | None = None,
    message: str | None = None,
) -> None:
    """Render command output respecting output_opts.

    Args:
        data: The structured data to output (dict, list, or scalar).
        output_opts: Output options from the decorator.
        human_formatter: Callable that prints human-readable output. If None,
            ``message`` is printed instead.
        message: Simple message for human output when no formatter is provided.
    """
    if output_opts.quiet and not output_opts.json_output:
        return

    if output_opts.json_output:
        if output_opts.fields and data is not None:
            data = _filter_fields(data, output_opts.fields)

        envelope: dict[str, Any] = {"data": data}
        if output_opts.verbosity >= 3:
            envelope["_request_id"] = output_opts.request_id

        click.echo(json.dumps(envelope, indent=2, default=str))
    else:
        if human_formatter is not None:
            human_formatter(data)
        elif message is not None:
            click.echo(message)
