"""API key creation and revocation utilities.

Extracted from server.auth.database_key to allow services layer
to manage API keys without importing from the server layer.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.bricks.auth.constants import get_hmac_secret

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# API key security constants
API_KEY_PREFIX = "sk-"
API_KEY_MIN_LENGTH = 32


def hash_api_key(key: str) -> str:
    """Hash API key using HMAC-SHA256 with per-install secret.

    Uses get_hmac_secret() for consistent hashing across all code
    paths (Issue #3062).

    Args:
        key: Raw API key string.

    Returns:
        HMAC-SHA256 hex digest.
    """
    secret = get_hmac_secret()
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
        zone_id: Optional zone identifier.
        is_admin: Whether this key has admin privileges.
        expires_at: Optional expiry datetime (UTC).
        inherit_permissions: Whether agent inherits owner's permissions.

    Returns:
        Tuple of (key_id, raw_key). Raw key is only returned once.
    """
    from nexus.storage.models import APIKeyModel

    final_subject_id = subject_id or user_id

    valid_subject_types = ["user", "agent", "service"]
    if subject_type not in valid_subject_types:
        raise ValueError(f"subject_type must be one of {valid_subject_types}, got {subject_type}")

    zone_prefix = f"{zone_id[:8]}_" if zone_id else ""
    subject_prefix = final_subject_id[:12] if subject_type == "agent" else user_id[:8]
    random_suffix = secrets.token_hex(16)
    key_id_part = secrets.token_hex(4)

    raw_key = f"{API_KEY_PREFIX}{zone_prefix}{subject_prefix}_{key_id_part}_{random_suffix}"
    key_hash = hash_api_key(raw_key)

    api_key = APIKeyModel(
        key_hash=key_hash,
        user_id=user_id,
        name=name,
        zone_id=zone_id,
        is_admin=int(is_admin),
        expires_at=expires_at,
        subject_type=subject_type,
        subject_id=final_subject_id,
        inherit_permissions=int(inherit_permissions),
    )

    session.add(api_key)
    session.flush()

    return (api_key.key_id, raw_key)


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
