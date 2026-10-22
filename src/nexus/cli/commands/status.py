"""Show the current native HTTP host and authenticated principal."""

from __future__ import annotations

import time

import click

from nexus.cli.clients.host import HostClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.theme import console
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.command()
@click.option(
    "--watch", is_flag=True, help="Refresh the authenticated native status every 2 seconds."
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=HostClient)
def status(client: HostClient, watch: bool) -> ServiceResult:
    """Read native status and whoami with the selected connection's credential."""
    if watch:
        ctx = click.get_current_context()
        if ctx.params["output_opts"].json_output:
            raise click.UsageError("--watch requires human output; omit --json")
    data = client.probe()
    if watch:
        from rich.live import Live
        from rich.pretty import Pretty

        with Live(Pretty(data), console=console, refresh_per_second=1) as live:
            while True:
                time.sleep(2)
                data = client.probe()
                live.update(Pretty(data))
    return ServiceResult(data=data, human_formatter=console.print)


def register_commands(cli: click.Group) -> None:
    cli.add_command(status)
