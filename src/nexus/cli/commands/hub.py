"""`nexus hub` command group — admin CLI for running a shared MCP hub.

This module is a thin UX layer over existing auth/zone plumbing. It is
expected to run on the hub host (direct DB access via NEXUS_DATABASE_URL).
Remote admin is tracked as a follow-up to issue #3784.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import click
from sqlalchemy import func, select

from nexus.cli.commands._hub_common import (
    format_table,
    get_session_factory,
    parse_duration,
)
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel, ZoneModel


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

        # Validate zone lifecycle state against the authoritative registry
        # so a typo ("proud" instead of "prod") or a deleted/terminating
        # zone can't silently mint a credential bound to a zone the
        # operator intended to isolate or remove (#3784 rounds 6/9).
        active_zone = (
            session.execute(
                select(ZoneModel)
                .where(ZoneModel.zone_id == zone_id)
                .where(ZoneModel.phase == "Active")
                .where(ZoneModel.deleted_at.is_(None))
            )
            .scalars()
            .first()
        )
        if active_zone is None:
            # Bootstrap escape: if the zones table is completely empty,
            # allow the first admin token to be minted for any zone so
            # a fresh hub can be bootstrapped before any zone is
            # created. After that, every --zone must refer to an active,
            # non-deleted row.
            any_zone = session.execute(select(ZoneModel).limit(1)).scalars().first()
            if any_zone is not None:
                known = [
                    z.zone_id
                    for z in session.execute(
                        select(ZoneModel)
                        .where(ZoneModel.phase == "Active")
                        .where(ZoneModel.deleted_at.is_(None))
                    )
                    .scalars()
                    .all()
                ]
                raise click.ClickException(
                    f"zone {zone_id!r} is not active (not found, deleted, or "
                    f"terminating). Active zones: "
                    f"{', '.join(sorted(known)) or '(none)'}. "
                    "Create it first with `nexus zone create` or use --zone <existing>."
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
    click.echo(body)


@token.command("revoke")
@click.argument("identifier")
def token_revoke(identifier: str) -> None:
    """Revoke a token by key_id prefix or name. Soft-delete (audit trail preserved)."""
    factory = get_session_factory()
    with factory() as session, session.begin():
        # Match by exact key_id, key_id prefix, or name (all must be non-revoked).
        matches = (
            session.execute(
                select(APIKeyModel)
                .where(APIKeyModel.revoked == 0)
                .where(
                    (APIKeyModel.key_id == identifier)
                    | (APIKeyModel.key_id.startswith(identifier))
                    | (APIKeyModel.name == identifier)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) == 0:
            raise click.ClickException(f"no active token matches {identifier!r}")
        if len(matches) > 1:
            names = ", ".join(f"{m.name} ({m.key_id})" for m in matches)
            click.echo(
                f"ambiguous: {len(matches)} tokens match {identifier!r} — {names}",
                err=True,
            )
            raise SystemExit(2)

        row = matches[0]
        row.revoked = 1
        row.revoked_at = datetime.now(UTC)

    click.echo(f"revoked {row.name} ({row.key_id}). Effective within 60s (auth cache TTL).")


@hub.group("zone")
def hub_zone() -> None:
    """Zone inspection (aliases existing `nexus zone` commands)."""


@hub_zone.command("list")
@click.pass_context
def hub_zone_list(ctx: click.Context) -> None:
    """List zones — alias of `nexus zone list`.

    `ctx.invoke` bypasses Click's option-parsing, so envvar defaults on the
    underlying `zone list` command (NEXUS_URL, NEXUS_API_KEY, NEXUS_DATA_DIR,
    etc.) are not picked up automatically. Forward them explicitly here so
    the alias behaves the same as running `nexus zone list` directly.
    """
    from nexus.cli.commands.zone import zone as _zone

    list_cmd = _zone.commands.get("list")
    if list_cmd is None:
        raise click.ClickException(
            "`nexus zone list` is not available — cannot delegate from `hub zone list`."
        )
    ctx.invoke(
        list_cmd,
        hostname=os.environ.get("NEXUS_HOSTNAME"),
        data_dir=os.environ.get("NEXUS_DATA_DIR", "./nexus-data/zones"),
        bind=os.environ.get("NEXUS_BIND_ADDR", "0.0.0.0:2028"),
        remote_url=os.environ.get("NEXUS_URL"),
        remote_api_key=os.environ.get("NEXUS_API_KEY"),
        json_output=False,
        quiet=False,
        verbosity=0,
        fields=None,
    )


def _read_redis_stats() -> dict[str, Any]:
    """Read the `nexus:hub:*` counters written by middleware_audit._record_metrics.

    Returns a dict with `qps_5m`, `connections`, `redis` ∈ {"ok", "n/a"}.
    """
    import time

    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url:
        return {"qps_5m": None, "connections": None, "redis": "n/a"}
    try:
        import redis  # synchronous client for CLI
    except ImportError:
        return {"qps_5m": None, "connections": None, "redis": "n/a"}

    try:
        client = redis.from_url(url, socket_timeout=2)
        client.ping()
        now_min = int(time.time()) // 60
        minute_keys = [f"nexus:hub:qps:{now_min - i}" for i in range(5)]
        active_key = f"nexus:hub:active:{now_min}"
        values = client.mget(minute_keys)
        total = sum(int(v) for v in values if v is not None)
        active = client.scard(active_key)
        return {
            "qps_5m": round(total / 300.0, 2),
            "connections": int(active),
            "redis": "ok",
        }
    except Exception:  # noqa: BLE001
        return {"qps_5m": None, "connections": None, "redis": "n/a"}


@hub.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def hub_status(as_json: bool) -> None:
    """Show hub health: postgres, redis, tokens, connections, qps."""
    import json as _json

    host = os.environ.get("NEXUS_MCP_HOST", "0.0.0.0")
    port = os.environ.get("NEXUS_MCP_PORT", "8081")
    profile = os.environ.get("NEXUS_PROFILE", "full")
    endpoint = f"http://{host}:{port}/mcp"

    pg_state = "ok"
    active = revoked = 0
    try:
        factory = get_session_factory()
        with factory() as session:
            active = (
                session.execute(
                    select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 0)
                ).scalar()
                or 0
            )
            revoked = (
                session.execute(
                    select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 1)
                ).scalar()
                or 0
            )
    except Exception:  # noqa: BLE001
        pg_state = "err"

    redis_stats = _read_redis_stats()

    payload = {
        "endpoint": endpoint,
        "profile": profile,
        "postgres": pg_state,
        "redis": redis_stats["redis"],
        "tokens": {"active": int(active), "revoked": int(revoked)},
        "connections": redis_stats["connections"],
        "qps_5m": redis_stats["qps_5m"],
    }

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
    else:
        click.echo(f"endpoint:    {payload['endpoint']}")
        click.echo(f"profile:     {payload['profile']}")
        click.echo(f"postgres:    {payload['postgres']}")
        click.echo(f"redis:       {payload['redis']}")
        click.echo(
            f"tokens:      {payload['tokens']['active']} active, "
            f"{payload['tokens']['revoked']} revoked"
        )
        click.echo(
            "connections: "
            f"{payload['connections'] if payload['connections'] is not None else 'n/a'}"
        )
        click.echo(f"qps (5m):    {payload['qps_5m'] if payload['qps_5m'] is not None else 'n/a'}")

    # Postgres is the source of truth for tokens and zones. A broken
    # auth DB must block rollout / trip paging — exit non-zero so
    # shell-style health guards (`nexus hub status && …`) fail closed
    # instead of reading "ok" off a silently-green exit code (#3784
    # round 9). Redis is best-effort (metrics only) so we don't fail
    # on `redis: n/a`.
    if pg_state != "ok":
        raise SystemExit(2)
