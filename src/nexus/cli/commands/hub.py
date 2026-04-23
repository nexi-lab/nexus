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
    format_table,
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

    with factory() as session, session.begin():
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

    click.echo(f"key_id: {key_id}")
    click.echo(f"token:  {raw_key}")
    click.echo("")
    click.echo("Save this token now — it will not be shown again.")


@token.command("list")
@click.option(
    "--show-revoked",
    "show_revoked",
    is_flag=True,
    help="Include revoked tokens in the output.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit JSON instead of a text table.",
)
def token_list(show_revoked: bool, as_json: bool) -> None:
    """List tokens (active by default)."""
    import json as _json

    factory = get_session_factory()
    with factory() as session:
        stmt = select(APIKeyModel).order_by(APIKeyModel.created_at.desc())
        if not show_revoked:
            stmt = stmt.where(APIKeyModel.revoked == 0)
        rows = session.execute(stmt).scalars().all()

    def _iso(dt: datetime | None) -> str:
        return dt.isoformat() if dt else "-"

    if as_json:
        payload = {
            "tokens": [
                {
                    "key_id": r.key_id,
                    "name": r.name,
                    "zone": r.zone_id,
                    "admin": bool(r.is_admin),
                    "created": _iso(r.created_at),
                    "last_used": _iso(r.last_used_at),
                    "revoked": bool(r.revoked),
                    "revoked_at": _iso(r.revoked_at),
                }
                for r in rows
            ]
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    body = format_table(
        headers=["key_id", "name", "zone", "admin", "created", "last_used", "revoked_at"],
        rows=[
            [
                r.key_id[:12] + "…" if len(r.key_id) > 12 else r.key_id,
                r.name,
                r.zone_id,
                "yes" if r.is_admin else "no",
                _iso(r.created_at),
                _iso(r.last_used_at),
                _iso(r.revoked_at),
            ]
            for r in rows
        ],
    )
    click.echo(body if body else "(no tokens)")
