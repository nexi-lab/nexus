"""Manage credentials through the native host's authenticated key API."""

from __future__ import annotations

import click

from nexus.cli.clients.auth import AuthClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.theme import console
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def admin() -> None:
    """Manage native credentials with an existing native admin key."""


@admin.command("create-key")
@click.argument("subject_id")
@click.option(
    "--subject-type", type=click.Choice(["user", "service"]), default="user", show_default=True
)
@click.option("--name", default="", help="Credential label.")
@click.option("--zone", "zones", multiple=True, help="Native zone grant, for example sharedzone:r.")
@click.option("--admin", is_flag=True, help="Create a zoneless administrator credential.")
@click.option(
    "--expires-at-ms",
    type=click.IntRange(min=0),
    default=0,
    help="Absolute Unix expiry in milliseconds; zero means no expiry.",
)
@click.option(
    "--allow-existing",
    is_flag=True,
    help="Allow another credential for this subject during rotation.",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AuthClient)
def create_key(
    client: AuthClient,
    subject_id: str,
    subject_type: str,
    name: str,
    zones: tuple[str, ...],
    admin: bool,
    expires_at_ms: int,
    allow_existing: bool,
) -> ServiceResult:
    """Mint a native key. The plaintext key is returned once."""
    data = client.mint_key(
        subject_type=subject_type,
        subject_id=subject_id,
        name=name,
        zones=zones,
        admin=admin,
        expires_at_ms=expires_at_ms,
        allow_existing=allow_existing,
    )
    return ServiceResult(data=data, human_formatter=console.print)


@admin.command("list-keys")
@click.option("--subject-type", type=click.Choice(["user", "service", "agent"]))
@click.option("--subject-id")
@click.option(
    "--is-admin", type=bool, default=None, help="Filter by the stored administrator flag."
)
@click.option("--include-revoked", is_flag=True)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AuthClient)
def list_keys(
    client: AuthClient,
    subject_type: str | None,
    subject_id: str | None,
    is_admin: bool | None,
    include_revoked: bool,
) -> ServiceResult:
    """List native key records without plaintext credentials."""
    data = client.list_keys(
        subject_type=subject_type,
        subject_id=subject_id,
        is_admin=is_admin,
        include_revoked=include_revoked,
    )
    return ServiceResult(data=data, human_formatter=console.print)


@admin.command("revoke-key")
@click.argument("key_hash")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AuthClient)
def revoke_key(client: AuthClient, key_hash: str) -> ServiceResult:
    """Revoke the key hash returned by create-key or list-keys."""
    return ServiceResult(data=client.revoke_key(key_hash), human_formatter=console.print)
