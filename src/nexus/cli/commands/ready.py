"""Wait for an authenticated native HTTP host within one deadline."""

from __future__ import annotations

import time

import click
import httpx

from nexus.cli.clients.base import NexusAPIError
from nexus.cli.clients.host import HostClient
from nexus.cli.config import resolve_connection
from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.theme import console
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.command()
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=60.0,
    show_default=True,
    help="Total deadline in seconds for native status and whoami.",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@click.pass_context
def ready(
    ctx: click.Context,
    output_opts: OutputOptions,
    timeout: float,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Wait for /v2/status and /v2/auth/whoami on the selected native HTTP host."""
    timing = CommandTiming()
    try:
        connection = resolve_connection(
            remote_url, remote_api_key, profile_name=(ctx.obj or {}).get("profile")
        )
        if not connection.url:
            render_error(
                error=click.UsageError("Set NEXUS_URL, --remote-url or a connection profile"),
                output_opts=output_opts,
                timing=timing,
                exit_code=ExitCode.CONFIG_ERROR,
            )
        deadline = time.monotonic() + timeout
        reason = "Native readiness deadline elapsed"
        successful = None
        with timing.phase("server"), HostClient(connection.url, connection.api_key) as client:
            while time.monotonic() < deadline:
                try:
                    data = client.probe(deadline=deadline)
                except NexusAPIError as exc:
                    if exc.status_code not in {429, 502, 503, 504}:
                        raise
                    reason = str(exc)
                except (httpx.HTTPError, TimeoutError) as exc:
                    reason = str(exc)
                else:
                    successful = data
                    break
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        if successful is not None:
            render_output(
                data={"ready": True, **successful},
                output_opts=output_opts,
                timing=timing,
                human_formatter=console.print,
            )
            return
        render_output(
            data={"ready": False, "server_url": connection.url, "reason": reason},
            output_opts=output_opts,
            timing=timing,
            human_formatter=console.print,
        )
        raise SystemExit(ExitCode.TEMPFAIL)
    except KeyboardInterrupt:
        raise SystemExit(ExitCode.INTERRUPTED) from None
    except Exception as exc:
        render_error(error=exc, output_opts=output_opts, timing=timing)


def register_commands(cli: click.Group) -> None:
    cli.add_command(ready)
