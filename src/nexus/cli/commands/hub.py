"""`nexus hub` command group — admin CLI for running a shared MCP hub.

This module is a thin UX layer over existing auth/zone plumbing. It is
expected to run on the hub host (direct DB access via NEXUS_DATABASE_URL).
Remote admin is tracked as a follow-up to issue #3784.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from sqlalchemy import func, select

from nexus.cli.commands._hub_common import (
    format_table,
    get_session_factory,
)
from nexus.cli.commands._hub_remote import HubRemoteError, call_hub_admin_tool
from nexus.contracts.zone_phase import ZonePhase
from nexus.hub.admin_ops import (
    HubAdminAmbiguousTargetError,
    HubAdminError,
    create_hub_token,
    list_hub_tokens,
    parse_zones_csv,
    revoke_hub_token,
)
from nexus.storage.api_key_ops import (
    add_zone_to_key,
    create_api_key,
    get_zone_perms_for_key,
    remove_zone_from_key,
)
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel

_VALID_PERMS = ("r", "w", "rw", "rwx")


def _parse_zones_csv(raw: str) -> list[str | tuple[str, str]]:
    """Parse ``"eng:rw,ops:r"`` into ``[("eng","rw"), ("ops","r")]``.

    Bare entries (no colon) default to ``"rw"`` and are returned as plain
    strings so ``create_api_key`` records the default uniformly.
    """
    try:
        return parse_zones_csv(raw)
    except HubAdminError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def hub() -> None:
    """Admin commands for running a shared nexus hub (issue #3784)."""


@hub.group()
def token() -> None:
    """Manage bearer tokens (api keys) for hub clients."""


@token.command("create")
@click.option("--name", required=True, help="Human-readable token name (unique).")
@click.option(
    "--zones",
    "zones_csv",
    default=None,
    help="Comma-separated zones the token can access (e.g. eng,ops or eng:rw,ops:r). "
    "Per-zone permissions (r|w|rw|rwx) may be appended after a colon; "
    "bare entries default to 'rw'.",
)
@click.option(
    "--zone",
    "zone_alias",
    default=None,
    hidden=True,
    help="Deprecated alias for --zones (single zone).",
)
@click.option(
    "--zones-glob",
    "zones_glob",
    default=None,
    help="Glob pattern resolved against active zones at mint time (e.g. 'team-*'). "
    "Mutually exclusive with --zones / --zone.",
)
@click.option("--admin", "is_admin", is_flag=True, help="Grant admin privileges.")
@click.option(
    "--expires",
    "expires",
    default=None,
    help="Expiry duration (e.g. 90d, 24h, 30m).",
)
@click.option("--user-id", default=None, help="Owner user_id. Defaults to --name.")
@click.option("--remote", default=None, help="Remote hub base URL or MCP URL.")
@click.option("--admin-token", default=None, help="Admin token for --remote.")
def token_create(
    name: str,
    zones_csv: str | None,
    zone_alias: str | None,
    zones_glob: str | None,
    is_admin: bool,
    expires: str | None,
    user_id: str | None,
    remote: str | None,
    admin_token: str | None,
) -> None:
    """Create a new bearer token. Prints the raw key once; not retrievable after."""
    sources = [s for s in (zones_csv, zone_alias, zones_glob) if s is not None]
    if len(sources) == 0:
        raise click.ClickException("One of --zones, --zone, or --zones-glob is required.")
    if len(sources) > 1:
        raise click.ClickException(
            "--zones, --zone, and --zones-glob are mutually exclusive; pass only one."
        )

    if remote:
        token = _resolve_remote_admin_token(admin_token, remote)
        payload = _call_remote_admin_tool(
            remote,
            token,
            "nexus_hub_token_create",
            {
                "name": name,
                "zones": zones_csv if zones_csv is not None else zone_alias,
                "zones_glob": zones_glob,
                "admin": is_admin,
                "expires": expires,
                "user_id": user_id,
            },
        )
        _render_token_create(payload)
        return

    factory = get_session_factory()
    try:
        payload = create_hub_token(
            factory,
            name=name,
            zones_csv=zones_csv if zones_csv is not None else zone_alias,
            zones_glob=zones_glob,
            is_admin=is_admin,
            expires=expires,
            user_id=user_id,
            create_api_key_fn=create_api_key,
        )
    except HubAdminError as exc:
        raise click.ClickException(str(exc)) from exc

    _render_token_create(payload)


def _render_token_create(payload: dict[str, Any]) -> None:
    click.echo(f"key_id: {payload['key_id']}")
    click.echo(f"token:  {payload['token']}")
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
@click.option("--remote", default=None, help="Remote hub base URL or MCP URL.")
@click.option("--admin-token", default=None, help="Admin token for --remote.")
def token_list(
    show_revoked: bool,
    as_json: bool,
    remote: str | None,
    admin_token: str | None,
) -> None:
    """List tokens (active by default)."""
    if remote:
        token = _resolve_remote_admin_token(admin_token, remote)
        payload = _call_remote_admin_tool(
            remote,
            token,
            "nexus_hub_token_list",
            {"show_revoked": show_revoked},
        )
        _render_token_list(payload, as_json=as_json)
        return

    factory = get_session_factory()
    payload = list_hub_tokens(factory, show_revoked=show_revoked)
    _render_token_list(payload, as_json=as_json)


def _render_token_list(payload: dict[str, Any], *, as_json: bool) -> None:
    import json as _json

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
        return
    body = format_table(
        headers=["key_id", "name", "zone", "zones", "admin", "created", "last_used", "revoked_at"],
        rows=[
            [
                row["key_id"][:12] + "…" if len(row["key_id"]) > 12 else row["key_id"],
                row["name"],
                row["zone"] or "-",
                ",".join(_zone_ids(row["zones"])),
                "yes" if row["admin"] else "no",
                row["created"],
                row["last_used"],
                row["revoked_at"],
            ]
            for row in payload["tokens"]
        ],
    )
    click.echo(body)


def _zone_ids(zones: list[Any]) -> list[str]:
    return [zone["zone_id"] if isinstance(zone, dict) else zone for zone in zones]


@token.command("revoke")
@click.argument("identifier")
@click.option("--remote", default=None, help="Remote hub base URL or MCP URL.")
@click.option("--admin-token", default=None, help="Admin token for --remote.")
def token_revoke(identifier: str, remote: str | None, admin_token: str | None) -> None:
    """Revoke a token by key_id prefix or name. Soft-delete (audit trail preserved)."""
    if remote:
        token = _resolve_remote_admin_token(admin_token, remote)
        payload = _call_remote_admin_tool(
            remote,
            token,
            "nexus_hub_token_revoke",
            {"identifier": identifier},
        )
        click.echo(payload["message"])
        return

    factory = get_session_factory()
    try:
        payload = revoke_hub_token(factory, identifier=identifier)
    except HubAdminAmbiguousTargetError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2) from exc
    except HubAdminError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(payload["message"])


@token.group("zones")
def token_zones() -> None:
    """Manage a token's zone allow-list (#3785)."""


def _resolve_token_by_name(session: Any, name: str) -> APIKeyModel:
    row: APIKeyModel | None = (
        session.execute(
            select(APIKeyModel).where(APIKeyModel.name == name).where(APIKeyModel.revoked == 0)
        )
        .scalars()
        .first()
    )
    if row is None:
        raise click.ClickException(f"no active token named {name!r}")
    return row


@token_zones.command("add")
@click.option("--name", required=True, help="Token name.")
@click.option("--zone", "zone_id", required=True, help="Zone to add.")
@click.option(
    "--perms",
    "permissions",
    type=click.Choice(_VALID_PERMS),
    default="rw",
    show_default=True,
    help="Per-zone permission (r|w|rw|rwx).",
)
def token_zones_add(name: str, zone_id: str, permissions: str) -> None:
    """Add a zone to a token's allow-list. Idempotent."""
    factory = get_session_factory()
    with factory() as session, session.begin():
        active = (
            session.execute(
                select(ZoneModel)
                .where(ZoneModel.zone_id == zone_id)
                .where(ZoneModel.phase == ZonePhase.ACTIVE)
                .where(ZoneModel.deleted_at.is_(None))
            )
            .scalars()
            .first()
        )
        if active is None:
            raise click.ClickException(
                f"zone {zone_id!r} is not active. Use `nexus zone create` first."
            )
        token_row = _resolve_token_by_name(session, name)
        try:
            added = add_zone_to_key(session, token_row.key_id, zone_id, permissions=permissions)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"{'added' if added else 'no change'}: {name} → {zone_id} ({permissions})")


@token_zones.command("remove")
@click.option("--name", required=True, help="Token name.")
@click.option("--zone", "zone_id", required=True, help="Zone to remove.")
def token_zones_remove(name: str, zone_id: str) -> None:
    """Remove a zone from a token's allow-list. Refuses to leave token zoneless."""
    factory = get_session_factory()
    with factory() as session, session.begin():
        token_row = _resolve_token_by_name(session, name)
        try:
            removed = remove_zone_from_key(session, token_row.key_id, zone_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"{'removed' if removed else 'no change'}: {name} → {zone_id}")


@token_zones.command("show")
@click.option("--name", required=True, help="Token name.")
def token_zones_show(name: str) -> None:
    """Print the token's zone allow-list with perms (primary first)."""
    factory = get_session_factory()
    with factory() as session:
        token_row = _resolve_token_by_name(session, name)
        pairs = get_zone_perms_for_key(session, token_row.key_id)
    primary = token_row.zone_id
    perms_by_zone = dict(pairs)
    ordered_ids = ([primary] if primary in perms_by_zone else []) + sorted(
        zid for zid in perms_by_zone if zid != primary
    )
    click.echo(
        format_table(
            headers=["zone", "perms"], rows=[[zid, perms_by_zone[zid]] for zid in ordered_ids]
        )
    )


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
@click.option(
    "--detail",
    is_flag=True,
    help="Include per-zone, per-token, rate-limit, and search detail.",
)
@click.option("--remote", default=None, help="Remote hub base URL or MCP URL.")
@click.option("--admin-token", default=None, help="Admin token for --remote.")
def hub_status(
    as_json: bool,
    detail: bool,
    remote: str | None,
    admin_token: str | None,
) -> None:
    """Show hub health: postgres, redis, tokens, connections, qps."""
    if remote:
        if detail:
            raise click.ClickException("--detail is only available for local hub status")
        token = _resolve_remote_admin_token(admin_token, remote)
        payload = _call_remote_admin_tool(remote, token, "nexus_hub_status", {})
        _render_status(payload, as_json=as_json)
        return

    postgres_status = _collect_postgres_status(detail=detail)
    payload = _base_status_payload(postgres_status)
    if detail:
        zone_ids = postgres_status["zone_ids"]
        redis_detail = _read_redis_detail_stats(zone_ids)
        payload["detail"] = True
        payload.update(
            _collect_status_detail(zone_ids, postgres_status["tokens_detail"], redis_detail)
        )

    _render_status(payload, as_json=as_json, detail=detail)


def _render_status(payload: dict[str, Any], *, as_json: bool, detail: bool = False) -> None:
    import json as _json

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
    else:
        _emit_base_status_text(payload)
        if detail:
            _emit_detail_status_text(payload)

    # Postgres is the source of truth for tokens and zones. A broken
    # auth DB must block rollout / trip paging — exit non-zero so
    # shell-style health guards (`nexus hub status && …`) fail closed
    # instead of reading "ok" off a silently-green exit code (#3784
    # round 9). Redis is best-effort (metrics only) so we don't fail
    # on `redis: n/a`.
    if payload["postgres"] != "ok":
        raise SystemExit(2)


def _display_status_value(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _iso_or_none(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _collect_zone_ids(session: Any) -> list[str]:
    rows = (
        session.execute(
            select(ZoneModel.zone_id)
            .where(ZoneModel.phase == ZonePhase.ACTIVE)
            .where(ZoneModel.deleted_at.is_(None))
            .order_by(ZoneModel.zone_id.asc())
        )
        .scalars()
        .all()
    )
    return [str(zone_id) for zone_id in rows]


def _token_zones_by_key(session: Any, key_ids: list[str]) -> dict[str, list[str]]:
    if not key_ids:
        return {}
    rows = session.execute(
        select(APIKeyZoneModel.key_id, APIKeyZoneModel.zone_id)
        .where(APIKeyZoneModel.key_id.in_(key_ids))
        .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
    ).all()
    zones_by_key: dict[str, list[str]] = {key_id: [] for key_id in key_ids}
    for key_id, zone_id in rows:
        zones_by_key.setdefault(key_id, []).append(zone_id)
    return zones_by_key


def _collect_token_detail(session: Any) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            select(APIKeyModel).order_by(APIKeyModel.created_at.desc(), APIKeyModel.key_id.asc())
        )
        .scalars()
        .all()
    )
    key_ids = [row.key_id for row in rows]
    zones_by_key = _token_zones_by_key(session, key_ids)
    return [
        {
            "key_id": row.key_id,
            "name": row.name,
            "zones": zones_by_key.get(row.key_id, []),
            "admin": bool(row.is_admin),
            "created": _iso_or_none(row.created_at),
            "last_seen": _iso_or_none(row.last_used_at),
            "revoked": bool(row.revoked),
            "revoked_at": _iso_or_none(row.revoked_at),
        }
        for row in rows
    ]


def _collect_postgres_status(detail: bool = False) -> dict[str, Any]:
    pg_state = "ok"
    active = revoked = 0
    zone_ids: list[str] = []
    tokens_detail: list[dict[str, Any]] = []
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
            if detail:
                zone_ids = _collect_zone_ids(session)
                tokens_detail = _collect_token_detail(session)
    except Exception:  # noqa: BLE001
        pg_state = "err"
    return {
        "postgres": pg_state,
        "tokens": {"active": int(active), "revoked": int(revoked)},
        "zone_ids": zone_ids,
        "tokens_detail": tokens_detail,
    }


def _base_status_payload(postgres_status: dict[str, Any]) -> dict[str, Any]:
    host = os.environ.get("NEXUS_MCP_HOST", "0.0.0.0")
    port = os.environ.get("NEXUS_MCP_PORT", "8081")
    profile = os.environ.get("NEXUS_PROFILE", "full")
    endpoint = f"http://{host}:{port}/mcp"

    redis_stats = _read_redis_stats()
    return {
        "endpoint": endpoint,
        "profile": profile,
        "postgres": postgres_status["postgres"],
        "redis": redis_stats["redis"],
        "tokens": postgres_status["tokens"],
        "connections": redis_stats["connections"],
        "qps_5m": redis_stats["qps_5m"],
    }


def _emit_base_status_text(payload: dict[str, Any]) -> None:
    click.echo(f"endpoint:    {payload['endpoint']}")
    click.echo(f"profile:     {payload['profile']}")
    click.echo(f"postgres:    {payload['postgres']}")
    click.echo(f"redis:       {payload['redis']}")
    click.echo(
        f"tokens:      {payload['tokens']['active']} active, {payload['tokens']['revoked']} revoked"
    )
    click.echo(f"connections: {_display_status_value(payload['connections'])}")
    click.echo(f"qps (5m):    {_display_status_value(payload['qps_5m'])}")


_RATE_LIMIT_TIERS = ("anonymous", "authenticated", "premium")


def _decode_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read_redis_detail_stats(zone_ids: list[str]) -> dict[str, Any]:
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    empty_zones = [{"zone_id": zone_id, "clients": None, "qps_5m": None} for zone_id in zone_ids]
    empty = {
        "zones": empty_zones,
        "rate_limits": {
            "window_seconds": 300,
            "hits_by_tier": dict.fromkeys(_RATE_LIMIT_TIERS),
        },
    }
    if not url:
        return empty
    try:
        import redis
    except ImportError:
        return empty

    client = None
    try:
        client = redis.from_url(url, socket_timeout=2)
        client.ping()
        now_min = int(time.time()) // 60
        zones: list[dict[str, Any]] = []
        for zone_id in zone_ids:
            minute_keys = [f"nexus:hub:qps:zone:{zone_id}:{now_min - i}" for i in range(5)]
            total = sum(_decode_int(v) for v in client.mget(minute_keys))
            active = client.scard(f"nexus:hub:active:zone:{zone_id}:{now_min}")
            zones.append(
                {
                    "zone_id": zone_id,
                    "clients": int(active),
                    "qps_5m": round(total / 300.0, 2),
                }
            )

        hits_by_tier: dict[str, int] = {}
        for tier in _RATE_LIMIT_TIERS:
            minute_keys = [f"nexus:hub:ratelimit:tier:{tier}:{now_min - i}" for i in range(5)]
            hits_by_tier[tier] = sum(_decode_int(v) for v in client.mget(minute_keys))

        return {
            "zones": zones,
            "rate_limits": {"window_seconds": 300, "hits_by_tier": hits_by_tier},
        }
    except Exception:  # noqa: BLE001
        return empty
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def _collect_status_detail(
    zone_ids: list[str],
    tokens_detail: list[dict[str, Any]],
    redis_detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "zones": redis_detail["zones"],
        "tokens_detail": tokens_detail,
        "rate_limits": redis_detail["rate_limits"],
        "search": _collect_search_detail(zone_ids),
    }


def _format_bytes(num_bytes: int | None) -> str | None:
    if num_bytes is None:
        return None
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KiB", "MiB", "GiB"):
        value /= 1024.0
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f} {unit}"
    return None


def _zone_index_path(base: Path, zone_id: str) -> Path | None:
    direct = base / zone_id
    try:
        if direct.exists():
            return direct
        if not base.is_dir():
            return None
        matches = [
            child
            for child in base.iterdir()
            if child.name.startswith(f"{zone_id}.") or child.name.startswith(f"{zone_id}-")
        ]
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _index_path_stats(path: Path) -> tuple[int | None, str | None]:
    try:
        if path.is_file():
            stat = path.stat()
            return stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()

        total_size = 0
        latest_mtime: float | None = None
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            stat = child.stat()
            total_size += stat.st_size
            latest_mtime = (
                stat.st_mtime if latest_mtime is None else max(latest_mtime, stat.st_mtime)
            )
    except OSError:
        return None, None

    last_indexed = (
        datetime.fromtimestamp(latest_mtime, tz=UTC).isoformat()
        if latest_mtime is not None
        else None
    )
    return total_size, last_indexed


def _zoekt_index_base() -> Path:
    raw = (
        os.environ.get("NEXUS_ZOEKT_INDEX_DIR")
        or os.environ.get("ZOEKT_INDEX_DIR")
        or "/app/data/.zoekt-index"
    )
    return Path(raw)


def _search_detail_row(
    zone_id: str,
    size_bytes: int | None,
    last_indexed: str | None,
) -> dict[str, Any]:
    return {
        "zone_id": zone_id,
        "zoekt_index_size_bytes": size_bytes,
        "zoekt_index_size_display": _format_bytes(size_bytes),
        "zoekt_last_indexed": last_indexed,
        "txtai_queue_depth": None,
        "last_indexed": last_indexed,
    }


def _collect_search_detail(zone_ids: list[str]) -> dict[str, Any]:
    base = _zoekt_index_base()
    zones = []
    matched_zone_index = False
    for zone_id in zone_ids:
        index_path = _zone_index_path(base, zone_id)
        size_bytes: int | None = None
        last_indexed: str | None = None
        if index_path is not None:
            matched_zone_index = True
            size_bytes, last_indexed = _index_path_stats(index_path)
        zones.append(_search_detail_row(zone_id, size_bytes, last_indexed))
    if not matched_zone_index and base.exists():
        size_bytes, last_indexed = _index_path_stats(base)
        if size_bytes is not None:
            zones.append(_search_detail_row("all", size_bytes, last_indexed))
    return {"zones": zones}


def _emit_detail_status_text(payload: dict[str, Any]) -> None:
    click.echo("")
    click.echo("zones:")
    click.echo(
        format_table(
            headers=["zone", "clients", "qps_5m"],
            rows=[
                [
                    row["zone_id"],
                    _display_status_value(row.get("clients")),
                    _display_status_value(row.get("qps_5m")),
                ]
                for row in payload.get("zones", [])
            ],
        )
    )
    click.echo("")
    click.echo("tokens:")
    click.echo(
        format_table(
            headers=["key_id", "name", "zones", "admin", "last_seen"],
            rows=[
                [
                    row["key_id"],
                    row["name"],
                    ",".join(row.get("zones", [])),
                    "yes" if row.get("admin") else "no",
                    _display_status_value(row.get("last_seen")),
                ]
                for row in payload.get("tokens_detail", [])
            ],
        )
    )
    click.echo("")
    click.echo("rate limits:")
    hits = payload.get("rate_limits", {}).get("hits_by_tier", {})
    click.echo(
        format_table(
            headers=["tier", "hits_5m"],
            rows=[[tier, _display_status_value(hits[tier])] for tier in sorted(hits)],
        )
    )
    click.echo("")
    click.echo("search:")
    click.echo(
        format_table(
            headers=[
                "zone",
                "zoekt_size",
                "zoekt_last_indexed",
                "txtai_queue_depth",
                "last_indexed",
            ],
            rows=[
                [
                    row["zone_id"],
                    _display_status_value(row.get("zoekt_index_size_display")),
                    _display_status_value(row.get("zoekt_last_indexed")),
                    _display_status_value(row.get("txtai_queue_depth")),
                    _display_status_value(row.get("last_indexed")),
                ]
                for row in payload.get("search", {}).get("zones", [])
            ],
        )
    )


def _resolve_remote_admin_token(admin_token: str | None, remote: str | None) -> str:
    if not remote:
        raise click.ClickException("internal error: remote token requested without --remote")
    token = admin_token or os.environ.get("NEXUS_HUB_ADMIN_TOKEN")
    if not token:
        raise click.ClickException(
            "--admin-token or NEXUS_HUB_ADMIN_TOKEN is required with --remote"
        )
    return token


def _call_remote_admin_tool(
    remote: str,
    admin_token: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        return call_hub_admin_tool(remote, admin_token, tool_name, arguments)
    except HubRemoteError as exc:
        raise click.ClickException(str(exc)) from exc
