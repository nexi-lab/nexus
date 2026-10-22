"""Decorator that eliminates boilerplate for service CLI commands.

Handles client creation, timing, output rendering, and error handling so that
individual commands only need to call the client and return a ``ServiceResult``.

Usage::

    @group.command("stats")
    @add_output_options
    @REMOTE_API_KEY_OPTION
    @REMOTE_URL_OPTION
    @service_command(client_class=SearchClient)
    def search_stats(client: SearchClient, ...) -> ServiceResult:
        data = client.stats()
        return ServiceResult(data=data, human_formatter=_render_stats)

The ``client_class`` parameter is required::

    @service_command(client_class=MyDomainClient)
    def my_command(client: MyDomainClient, ...) -> ServiceResult:
        ...
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import click

from nexus.cli.config import resolve_connection
from nexus.cli.output import OutputOptions, render_error, render_output
from nexus.cli.timing import CommandTiming


@dataclass(frozen=True)
class ServiceResult:
    """Immutable result returned by a ``@service_command``-decorated function.

    Attributes:
        data: Structured data to output (dict, list, or scalar).
        human_formatter: Callable that prints human-readable output. If *None*,
            ``message`` is printed instead.
        message: Simple message for human output when no formatter is provided.
    """

    data: Any
    human_formatter: Callable[[Any], None] | None = None
    message: str | None = None


def _validate_url(remote_url: str | None) -> str:
    """Validate that a server URL is provided, exit if not."""
    if not remote_url:
        from nexus.cli.exit_codes import ExitCode
        from nexus.cli.theme import console

        console.print(
            "[nexus.error]Error:[/nexus.error] Server URL required. Set NEXUS_URL or use --remote-url"
        )
        sys.exit(ExitCode.CONFIG_ERROR)
    return remote_url


def service_command(
    *,
    client_class: type,
) -> Any:
    """Decorator that wraps a service CLI command with timing, client, and output.

    Args:
        client_class: Domain-specific HTTP client class.

    The wrapped function receives a ``client`` keyword argument and must return
    a :class:`ServiceResult`.  The decorator handles:

    1. Validating the server URL.
    2. Creating a :class:`CommandTiming` instance.
    3. Opening the client inside a ``timing.phase("server")`` context.
    4. Calling the wrapped function with ``client`` and remaining kwargs.
    5. Passing the returned data through :func:`render_output`.
    6. Catching exceptions and routing them through :func:`render_error`.
    """

    def decorator(fn: Callable[..., ServiceResult]) -> Callable[..., None]:
        @functools.wraps(fn)
        def wrapper(
            output_opts: OutputOptions,
            remote_url: str | None,
            remote_api_key: str | None,
            **kwargs: Any,
        ) -> None:
            timing = CommandTiming()
            try:
                ctx = click.get_current_context(silent=True)
                profile = (ctx.obj or {}).get("profile") if ctx else None
                connection = resolve_connection(remote_url, remote_api_key, profile_name=profile)
                client = client_class(url=_validate_url(connection.url), api_key=connection.api_key)
                with timing.phase("server"), client:
                    result = fn(client=client, **kwargs)
                render_output(
                    data=result.data,
                    output_opts=output_opts,
                    timing=timing,
                    human_formatter=result.human_formatter,
                    message=result.message,
                )
            except SystemExit:
                raise
            except Exception as e:
                render_error(error=e, output_opts=output_opts, timing=timing)

        return wrapper

    return decorator
