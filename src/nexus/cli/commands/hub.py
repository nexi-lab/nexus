"""`nexus hub` command group — admin CLI for running a shared MCP hub.

This module is a thin UX layer over existing auth/zone plumbing. It is
expected to run on the hub host (direct DB access via NEXUS_DATABASE_URL).
Remote admin is tracked as a follow-up to issue #3784.
"""

from __future__ import annotations

from datetime import UTC, datetime

import click
from sqlalchemy import select

from nexus.cli.commands._hub_common import (
    get_session_factory,
    parse_duration,
)
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel


@click.group()
def hub() -> None:
    """Admin commands for running a shared nexus hub (issue #3784)."""


@hub.group()
def token() -> None:
    """Manage bearer tokens (api keys) for hub clients."""


@token.command("create")
@click.option("--name", required=True, help="Human-readable token name (unique).")
@click.option("--zone", "zone_id", required=True, help="Zone the token can access.")
@click.option("--admin", "is_admin", is_flag=True, help="Grant admin privileges.")
@click.option(
    "--expires",
    "expires",
    default=None,
    help="Expiry duration (e.g. 90d, 24h, 30m).",
)
@click.option("--user-id", default=None, help="Owner user_id. Defaults to --name.")
def token_create(
    name: str,
    zone_id: str,
    is_admin: bool,
    expires: str | None,
    user_id: str | None,
) -> None:
    """Create a new bearer token. Prints the raw key once; not retrievable after."""
    factory = get_session_factory()
    expires_at: datetime | None = None
    if expires:
        try:
            expires_at = datetime.now(UTC) + parse_duration(expires)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    with factory() as session:
        existing = (
            session.execute(
                select(APIKeyModel).where(APIKeyModel.name == name).where(APIKeyModel.revoked == 0)
            )
            .scalars()
            .first()
        )
        if existing is not None and getattr(existing, "name", None) == name:
            raise click.ClickException(
                f"token named {name!r} already exists (key_id={existing.key_id}). "
                "Revoke it first or use a different --name."
            )

        key_id, raw_key = create_api_key(
            session,
            user_id=user_id or name,
            name=name,
            zone_id=zone_id,
            is_admin=is_admin,
            expires_at=expires_at,
        )
        session.commit()

    click.echo(f"key_id: {key_id}")
    click.echo(f"token:  {raw_key}")
    click.echo("")
    click.echo("Save this token now — it will not be shown again.")
