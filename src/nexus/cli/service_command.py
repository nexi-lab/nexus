"""Decorator that eliminates boilerplate for service CLI commands.

Handles client creation, timing, output rendering, and error handling so that
individual commands only need to call the client and return a ``ServiceResult``.

Usage::

    @group.command("balance")
    @add_output_options
    @REMOTE_API_KEY_OPTION
    @REMOTE_URL_OPTION
    @service_command(client_class=PayClient)
    def pay_balance(client: PayClient, ...) -> ServiceResult:
        data = client.balance()
        return ServiceResult(data=data, human_formatter=_render_balance)

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
    func: Callable[..., ServiceResult] | None = None,
    *,
    client_class: type | None = None,
) -> Any:
    """Decorator that wraps a service CLI command with timing, client, and output.

    Args:
        func: The command function (when used without arguments).
        client_class: Domain-specific client class (e.g. IdentityClient). Required.

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
            url = _validate_url(remote_url)
            timing = CommandTiming()
            try:
                cls = client_class
                if cls is None:
                    raise ValueError(
                        "service_command requires client_class parameter — "
                        "NexusServiceClient has been removed (Issue #1133)"
                    )
                client = cls(url=url, api_key=remote_api_key)
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

    if func is not None:
        # Used as @service_command without arguments
        return decorator(func)
    # Used as @service_command(client_class=...) with arguments
    return decorator
