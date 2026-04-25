"""API key creation and revocation utilities.

Extracted from server.auth.database_key to allow services layer
to manage API keys without importing from the server layer.

Note: This module lives in the kernel (storage) layer and must NOT
import from the bricks layer.  The HMAC secret env var is read
directly here to stay consistent with bricks.auth.constants without
creating a cross-layer dependency (Issue #3062).
"""

import hashlib
import hmac
import logging
import os
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# API key security constants
API_KEY_PREFIX = "sk-"
API_KEY_MIN_LENGTH = 32
_HMAC_SALT_DEFAULT = "nexus-api-key-v1"

# Allowed per-zone permission strings (#3785).
_VALID_PERMISSIONS = frozenset({"r", "w", "rw", "rwx"})


def _validate_permissions(perms: str) -> str:
    if perms not in _VALID_PERMISSIONS:
        raise ValueError(f"invalid permissions {perms!r}; expected one of r, w, rw, rwx")
    return perms


def _get_hmac_secret() -> str:
    """Return the HMAC secret for API key hashing.

    Reads from NEXUS_API_KEY_SECRET env var for per-install isolation.
    Falls back to the legacy hardcoded salt for backward compat.
    Mirrors nexus.bricks.auth.constants.get_hmac_secret() without
    importing from the bricks layer.
    """
    return os.environ.get("NEXUS_API_KEY_SECRET", _HMAC_SALT_DEFAULT)


def hash_api_key(key: str) -> str:
    """Hash API key using HMAC-SHA256 with per-install secret.

    Args:
        key: Raw API key string.

    Returns:
        HMAC-SHA256 hex digest.
    """
    secret = _get_hmac_secret()
    return hmac.new(
        secret.encode("utf-8"),
        key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_key_format(key: str) -> bool:
    """Validate API key format (prefix + minimum length).

    Args:
        key: Raw API key string.

    Returns:
        True if the key has valid format.
    """
    if not key.startswith(API_KEY_PREFIX):
        return False
    return len(key) >= API_KEY_MIN_LENGTH


def create_api_key(
    session: "Session",
    user_id: str,
    name: str,
    subject_type: str = "user",
    subject_id: str | None = None,
    *,
    zones: list[str | tuple[str, str]] | None = None,
    zone_id: str | None = None,
    is_admin: bool = False,
    expires_at: datetime | None = None,
    inherit_permissions: bool = False,
) -> tuple[str, str]:
    """Create a new API key in the database.

    Args:
        session: SQLAlchemy session.
        user_id: User identifier (owner of the key).
        name: Human-readable key name.
        subject_type: Type of subject ("user", "agent", or "service").
        subject_id: Custom subject ID (for agents). Defaults to user_id.
        zones: List of zone identifiers for this key (#3785). Each entry is
            either a bare zone_id string (defaulting to ``"rw"`` permissions)
            or a ``(zone_id, perms)`` tuple where perms is one of
            ``"r" | "w" | "rw" | "rwx"``. The first zone is also written to
            ``APIKeyModel.zone_id`` as a deprecated backfill alias so legacy
            ``WHERE zone_id = ?`` filters keep matching the primary; new
            readers should use the junction (``get_zones_for_key`` /
            ``get_zone_perms_for_key``). One junction row is written per
            zone with its perms. Takes precedence over ``zone_id`` when both
            are supplied.
        zone_id: Legacy single-zone identifier. Kept for backward compat;
            prefer ``zones`` for new callers. Ignored when ``zones`` is set.
        is_admin: Whether this key has admin privileges.
        expires_at: Optional expiry datetime (UTC).
        inherit_permissions: Whether agent inherits owner's permissions.

    Returns:
        Tuple of (key_id, raw_key). Raw key is only returned once.

    Raises:
        ValueError: If ``zones`` is explicitly passed as an empty list, if
            ``subject_type`` is invalid, or if any per-zone permission
            string is not one of ``r | w | rw | rwx``. Passing neither
            ``zones`` nor ``zone_id`` is still allowed (zone-less key,
            backward compat).
    """
    from nexus.storage.models import APIKeyModel, APIKeyZoneModel

    # Resolve effective zone list; zones= wins over legacy zone_id=
    zone_perms: list[tuple[str, str]]
    if zones is None:
        zone_perms = [(zone_id, "rw")] if zone_id else []
    elif len(zones) == 0:
        raise ValueError("create_api_key: zones list must not be empty")
    else:
        zone_perms = []
        for entry in zones:
            if isinstance(entry, tuple):
                zid, perms = entry
                _validate_permissions(perms)
                zone_perms.append((zid, perms))
            else:
                zone_perms.append((entry, "rw"))
    primary_zone = zone_perms[0][0] if zone_perms else None

    final_subject_id = subject_id or user_id

    valid_subject_types = ["user", "agent", "service"]
    if subject_type not in valid_subject_types:
        raise ValueError(f"subject_type must be one of {valid_subject_types}, got {subject_type}")

    zone_prefix = f"{primary_zone[:8]}_" if primary_zone else ""
    subject_prefix = final_subject_id[:12] if subject_type == "agent" else user_id[:8]
    random_suffix = secrets.token_hex(16)
    key_id_part = secrets.token_hex(4)

    raw_key = f"{API_KEY_PREFIX}{zone_prefix}{subject_prefix}_{key_id_part}_{random_suffix}"
    key_hash = hash_api_key(raw_key)

    api_key = APIKeyModel(
        key_hash=key_hash,
        user_id=user_id,
        name=name,
        zone_id=primary_zone,
        is_admin=int(is_admin),
        expires_at=expires_at,
        subject_type=subject_type,
        subject_id=final_subject_id,
        inherit_permissions=int(inherit_permissions),
    )

    session.add(api_key)
    session.flush()  # populate api_key.key_id before junction inserts

    for zid, perms in zone_perms:
        session.add(APIKeyZoneModel(key_id=api_key.key_id, zone_id=zid, permissions=perms))

    return (api_key.key_id, raw_key)


def create_agent_api_key(
    session: "Session",
    agent_id: str,
    agent_name: str,
    owner_id: str,
    zone_id: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    """Create an API key for an agent identity.

    Thin wrapper around ``create_api_key`` that hardcodes
    ``subject_type="agent"`` and wires the agent_id as subject_id.

    Used by both AgentRegistrationService and DelegationService to
    avoid duplicating the key-creation pattern (Issue #3130).

    Args:
        session: SQLAlchemy session (caller manages commit/rollback).
        agent_id: Unique agent identifier (becomes subject_id on the key).
        agent_name: Human-readable name for the key label.
        owner_id: User ID who owns the agent (becomes user_id on the key).
        zone_id: Optional zone identifier.
        expires_at: Optional expiry (None = permanent key).

    Returns:
        Tuple of (key_id, raw_key). Raw key is only returned once.
    """
    return create_api_key(
        session,
        user_id=owner_id,
        name=f"agent:{agent_name}",
        subject_type="agent",
        subject_id=agent_id,
        zone_id=zone_id,
        expires_at=expires_at,
    )


def revoke_api_key(session: "Session", key_id: str) -> bool:
    """Revoke an API key by key_id.

    Args:
        session: SQLAlchemy session.
        key_id: Key ID to revoke.

    Returns:
        True if key was revoked, False if not found.
    """
    from sqlalchemy import select

    from nexus.storage.models import APIKeyModel

    stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
    api_key = session.scalar(stmt)

    if not api_key:
        return False

    api_key.revoked = 1
    api_key.revoked_at = datetime.now(UTC)
    session.flush()

    return True


def get_zones_for_key(session: "Session", key_id: str) -> list[str]:
    """Return the full zone allow-list for a token (#3785)."""
    from sqlalchemy import select

    from nexus.storage.models import APIKeyZoneModel

    rows = (
        session.execute(select(APIKeyZoneModel.zone_id).where(APIKeyZoneModel.key_id == key_id))
        .scalars()
        .all()
    )
    return list(rows)


def get_zone_perms_for_key(session: "Session", key_id: str) -> list[tuple[str, str]]:
    """Return ``(zone_id, perms)`` pairs for a token's allow-list (#3785)."""
    from sqlalchemy import select

    from nexus.storage.models import APIKeyZoneModel

    rows = session.execute(
        select(APIKeyZoneModel.zone_id, APIKeyZoneModel.permissions).where(
            APIKeyZoneModel.key_id == key_id
        )
    ).all()
    return [(zid, perms) for zid, perms in rows]


def get_primary_zone(session: "Session", key_id: str) -> str | None:
    """Return the token's primary zone, or None if it has no zones.

    Primary = the row with the smallest granted_at. Ties broken by zone_id ASC
    so the result is deterministic across snapshots and replays.

    Replaces direct reads of the deprecated APIKeyModel.zone_id column (#3871).
    """
    from sqlalchemy import select

    from nexus.storage.models import APIKeyZoneModel

    stmt = (
        select(APIKeyZoneModel.zone_id)
        .where(APIKeyZoneModel.key_id == key_id)
        .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_primary_zones_for_keys(session: "Session", key_ids: list[str]) -> dict[str, str]:
    """Batch variant of get_primary_zone for renderers walking many rows.

    Single round-trip via a window function. Returns {key_id: primary_zone};
    zoneless keys are absent from the dict.
    """
    if not key_ids:
        return {}
    from sqlalchemy import func, select

    from nexus.storage.models import APIKeyZoneModel

    rn = (
        func.row_number()
        .over(
            partition_by=APIKeyZoneModel.key_id,
            order_by=(
                APIKeyZoneModel.granted_at.asc(),
                APIKeyZoneModel.zone_id.asc(),
            ),
        )
        .label("rn")
    )
    inner = (
        select(APIKeyZoneModel.key_id, APIKeyZoneModel.zone_id, rn)
        .where(APIKeyZoneModel.key_id.in_(key_ids))
        .subquery()
    )
    stmt = select(inner.c.key_id, inner.c.zone_id).where(inner.c.rn == 1)
    return {row.key_id: row.zone_id for row in session.execute(stmt)}


def add_zone_to_key(session: "Session", key_id: str, zone_id: str, permissions: str = "rw") -> bool:
    """Add a zone to a token's allow-list. Idempotent — returns False if already present."""
    from nexus.storage.models import APIKeyZoneModel

    _validate_permissions(permissions)
    existing = session.get(APIKeyZoneModel, (key_id, zone_id))
    if existing is not None:
        return False
    session.add(APIKeyZoneModel(key_id=key_id, zone_id=zone_id, permissions=permissions))
    return True


def remove_zone_from_key(session: "Session", key_id: str, zone_id: str) -> bool:
    """Remove a zone. Refuses to leave a token with zero zones (raises ValueError)."""
    from nexus.storage.models import APIKeyZoneModel

    current = get_zone_perms_for_key(session, key_id)
    current_ids = [zid for zid, _ in current]
    if zone_id not in current_ids:
        return False
    if len(current) == 1:
        raise ValueError(
            f"refusing to remove last zone {zone_id!r} from key {key_id!r}; "
            "revoke the token instead"
        )
    row = session.get(APIKeyZoneModel, (key_id, zone_id))
    session.delete(row)
    return True
