"""Show the native identity authenticated by the current credential."""

import click

from nexus.cli.clients.auth import AuthClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.theme import console
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def identity() -> None:
    """Inspect the authenticated native principal."""


@identity.command("whoami")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AuthClient)
def whoami(client: AuthClient) -> ServiceResult:
    """Read the principal from the native host's shared auth context."""
    return ServiceResult(data=client.whoami(), human_formatter=console.print)
