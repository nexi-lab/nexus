"""DB-backed hub administration operations."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from nexus.cli.commands._hub_common import parse_duration
from nexus.storage.api_key_ops import (
    create_api_key,
    get_primary_zones_for_keys,
    get_zone_perms_for_key,
)
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel

_VALID_PERMS = ("r", "w", "rw", "rwx")


class HubAdminError(Exception):
    """Raised for user-facing hub admin failures."""


class HubAdminAmbiguousTargetError(HubAdminError):
    """Raised when a revoke target matches multiple active keys."""

    def __init__(self, identifier: str, matches: list[tuple[str, str]]) -> None:
        self.identifier = identifier
        self.matches = matches
        names = ", ".join(f"{name} ({key_id})" for name, key_id in matches)
        super().__init__(f"ambiguous: {len(matches)} tokens match {identifier!r} - {names}")


def get_env_session_factory() -> Callable[[], Session]:
    """Build a hub DB session factory from the server environment."""
    from nexus.core.db_utils import normalize_database_url

    # Issue #4238: accept the canonical ``postgres://`` scheme.
    db_url = normalize_database_url(
        os.environ.get("NEXUS_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not db_url:
        from nexus.contracts.exceptions import ConfigurationError

        raise ConfigurationError(
            "Hub admin operations require a database-backed auth provider or "
            "NEXUS_DATABASE_URL/POSTGRES_URL/DATABASE_URL to be set"
        )
    return _build_env_session_factory(_sync_database_url(db_url))


@lru_cache(maxsize=4)
def _build_env_session_factory(db_url: str) -> Callable[[], Session]:
    engine = create_engine(db_url, future=True)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _sync_database_url(db_url: str) -> str:
    return db_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def parse_zones_csv(raw: str) -> list[str | tuple[str, str]]:
    """Parse ``eng:rw,ops:r`` into the shape accepted by ``create_api_key``."""
    out: list[str | tuple[str, str]] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if ":" in item:
            zid, perms = item.split(":", 1)
            zid = zid.strip()
            perms = perms.strip()
            if not zid:
                raise HubAdminError(f"empty zone id in {chunk!r}")
            if perms not in _VALID_PERMS:
                raise HubAdminError(
                    f"invalid permissions {perms!r} for zone {zid!r}; "
                    f"expected one of {', '.join(_VALID_PERMS)}"
                )
            out.append((zid, perms))
        else:
            out.append(item)
    return out


def parse_expires_at(raw: str | None) -> datetime | None:
    """Parse a relative duration into an absolute UTC expiry."""
    if raw is None:
        return None
    try:
        return datetime.now(UTC) + parse_duration(raw)
    except ValueError as exc:
        raise HubAdminError(str(exc)) from exc


def create_hub_token(
    session_factory: Callable[[], Any],
    *,
    name: str,
    zones_csv: str | None,
    zones_glob: str | None,
    is_admin: bool,
    expires: str | None,
    user_id: str | None,
    create_api_key_fn: Callable[..., tuple[str, str]] = create_api_key,
) -> dict[str, Any]:
    """Create a hub token and return the one-time raw token payload."""
    if zones_csv is None and zones_glob is None:
        raise HubAdminError("One of --zones, --zone, or --zones-glob is required.")
    if zones_csv is not None and zones_glob is not None:
        raise HubAdminError(
            "--zones, --zone, and --zones-glob are mutually exclusive; pass only one."
        )

    expires_at = parse_expires_at(expires)

    with session_factory() as session, session.begin():
        existing = (
            session.execute(
                select(APIKeyModel).where(APIKeyModel.name == name).where(APIKeyModel.revoked == 0)
            )
            .scalars()
            .first()
        )
        if existing is not None and getattr(existing, "name", None) == name:
            raise HubAdminError(
                f"token named {name!r} already exists (key_id={existing.key_id}). "
                "Revoke it first or use a different --name."
            )

        any_zone = session.execute(select(ZoneModel).limit(1)).scalars().first()
        zones = _resolve_create_zones(
            session,
            any_zone=any_zone,
            zones_csv=zones_csv,
            zones_glob=zones_glob,
        )

        if any_zone is None:
            for entry in zones:
                zid = entry[0] if isinstance(entry, tuple) else entry
                if not session.scalar(select(ZoneModel).where(ZoneModel.zone_id == zid)):
                    session.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
            session.flush()

        try:
            key_id, raw_key = create_api_key_fn(
                session,
                user_id=user_id or name,
                name=name,
                zones=zones,
                is_admin=is_admin,
                expires_at=expires_at,
            )
        except ValueError as exc:
            raise HubAdminError(str(exc)) from exc

        zone_payload = [
            {
                "zone_id": entry[0] if isinstance(entry, tuple) else entry,
                "permission": entry[1] if isinstance(entry, tuple) else "rw",
            }
            for entry in zones
        ]

    return {
        "key_id": key_id,
        "token": raw_key,
        "name": name,
        "admin": bool(is_admin),
        "zones": zone_payload,
    }


def _resolve_create_zones(
    session: Any,
    *,
    any_zone: ZoneModel | None,
    zones_csv: str | None,
    zones_glob: str | None,
) -> list[str | tuple[str, str]]:
    if zones_glob is not None:
        active = (
            session.execute(
                select(ZoneModel)
                .where(ZoneModel.phase == "Active")
                .where(ZoneModel.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        matched = sorted(z.zone_id for z in active if fnmatch.fnmatch(z.zone_id, zones_glob))
        if not matched:
            known = [z.zone_id for z in active]
            raise HubAdminError(
                f"--zones-glob {zones_glob!r}: no active zones match this pattern. "
                f"Active zones: {', '.join(sorted(known)) or '(none)'}."
            )
        return list(matched)

    zones = parse_zones_csv(zones_csv or "")
    if not zones:
        raise HubAdminError("--zones must contain at least one non-empty zone.")

    if any_zone is not None:
        _validate_active_zones(session, zones)
    return zones


def _validate_active_zones(session: Any, zones: list[str | tuple[str, str]]) -> None:
    for entry in zones:
        zid = entry[0] if isinstance(entry, tuple) else entry
        active_zone = (
            session.execute(
                select(ZoneModel)
                .where(ZoneModel.zone_id == zid)
                .where(ZoneModel.phase == "Active")
                .where(ZoneModel.deleted_at.is_(None))
            )
            .scalars()
            .first()
        )
        if active_zone is not None:
            continue
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
        raise HubAdminError(
            f"zone {zid!r} is not active (not found, deleted, or terminating). "
            f"Active zones: {', '.join(sorted(known)) or '(none)'}. "
            "Create it first with `nexus zone create` or use --zones <existing>."
        )


def list_hub_tokens(
    session_factory: Callable[[], Any],
    *,
    show_revoked: bool,
) -> dict[str, Any]:
    """Return token rows in the existing local CLI JSON payload shape."""
    with session_factory() as session:
        stmt = select(APIKeyModel).order_by(APIKeyModel.created_at.desc())
        if not show_revoked:
            stmt = stmt.where(APIKeyModel.revoked == 0)
        rows = session.execute(stmt).scalars().all()

        key_ids = [r.key_id for r in rows]
        junction_rows: list[APIKeyZoneModel] = []
        if key_ids:
            junction_rows = list(
                session.execute(select(APIKeyZoneModel).where(APIKeyZoneModel.key_id.in_(key_ids)))
                .scalars()
                .all()
            )

        primary_by_key: dict[str, str | None] = dict.fromkeys(key_ids)
        if key_ids:
            primary_by_key.update(get_primary_zones_for_keys(session, key_ids))

    zones_by_key: dict[str, list[str]] = {}
    for jr in junction_rows:
        zones_by_key.setdefault(jr.key_id, []).append(jr.zone_id)
    for kid in zones_by_key:
        primary = primary_by_key.get(kid)
        others = sorted(z for z in zones_by_key[kid] if z != primary)
        zones_by_key[kid] = ([primary] if primary else []) + others

    def _zones(row: APIKeyModel) -> list[str]:
        if row.key_id in zones_by_key:
            return zones_by_key[row.key_id]
        fallback = primary_by_key.get(row.key_id)
        return [fallback] if fallback is not None else []

    return {
        "tokens": [
            {
                "key_id": row.key_id,
                "name": row.name,
                "zone": primary_by_key.get(row.key_id),
                "zones": _zones(row),
                "admin": bool(row.is_admin),
                "created": _iso(row.created_at),
                "last_used": _iso(row.last_used_at),
                "revoked": bool(row.revoked),
                "revoked_at": _iso(row.revoked_at),
            }
            for row in rows
        ]
    }


def revoke_hub_token(session_factory: Callable[[], Any], *, identifier: str) -> dict[str, Any]:
    """Revoke a token by exact key id, key id prefix, or name."""
    with session_factory() as session, session.begin():
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
            raise HubAdminError(f"no active token matches {identifier!r}")
        if len(matches) > 1:
            raise HubAdminAmbiguousTargetError(
                identifier,
                [(match.name, match.key_id) for match in matches],
            )

        row = matches[0]
        row.revoked = 1
        row.revoked_at = datetime.now(UTC)
        key_id = row.key_id
        name = row.name

    message = f"revoked {name} ({key_id}). Effective within 60s (auth cache TTL)."
    return {"key_id": key_id, "name": name, "message": message}


def get_hub_status(
    session_factory: Callable[[], Any],
    *,
    redis_stats: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return hub health information used by local and remote CLI output."""
    host = os.environ.get("NEXUS_MCP_HOST", "0.0.0.0")
    port = os.environ.get("NEXUS_MCP_PORT", "8081")
    profile = os.environ.get("NEXUS_PROFILE", "full")
    endpoint = f"http://{host}:{port}/mcp"

    pg_state = "ok"
    active = revoked = 0
    try:
        with session_factory() as session:
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

    metrics = redis_stats() if redis_stats is not None else read_redis_stats()
    return {
        "endpoint": endpoint,
        "profile": profile,
        "postgres": pg_state,
        "redis": metrics["redis"],
        "tokens": {"active": int(active), "revoked": int(revoked)},
        "connections": metrics.get("connections"),
        "qps_5m": metrics.get("qps_5m"),
    }


def read_redis_stats() -> dict[str, Any]:
    """Read best-effort hub metrics from Redis/Dragonfly."""
    import time

    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url:
        return {"qps_5m": None, "connections": None, "redis": "n/a"}
    try:
        import redis
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


def get_zone_payload_for_key(session: Any, key_id: str) -> list[dict[str, str]]:
    """Return zone permissions for a token, primary zone first."""
    return [
        {"zone_id": zone_id, "permission": permission}
        for zone_id, permission in get_zone_perms_for_key(session, key_id)
    ]


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else "-"
