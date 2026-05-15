"""Immutable DTOs for auth data that crosses brick boundaries.

These frozen dataclasses define the shape of data exchanged between
the auth brick and its store implementations.  They never import
ORM models — keeping the auth brick storage-agnostic.

Issue #2436: Move auth/ to bricks/auth/ with Protocol DI.
"""

from dataclasses import dataclass
from datetime import datetime

from nexus.contracts.constants import ROOT_ZONE_ID


@dataclass(frozen=True)
class UserDTO:
    """Immutable user data transfer object."""

    user_id: str
    email: str | None = None
    username: str | None = None
    display_name: str | None = None
    is_active: bool = True
    email_verified: bool = False
    zone_id: str | None = None
    avatar_url: str | None = None
    user_metadata: str | None = None
    password_hash: str | None = None
    primary_auth_method: str | None = None
    is_global_admin: bool = False
    api_key: str | None = None
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class APIKeyDTO:
    """Immutable API key data transfer object."""

    key_id: str
    key_hash: str
    user_id: str
    name: str
    subject_type: str = "user"
    subject_id: str | None = None
    zone_id: str | None = None
    is_admin: bool = False
    expires_at: datetime | None = None
    revoked: bool = False
    inherit_permissions: bool = False
    last_used_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class OAuthCredentialDTO:
    """Immutable OAuth credential metadata (no encrypted tokens)."""

    credential_id: str
    provider: str
    user_email: str
    zone_id: str
    user_id: str | None = None
    token_type: str | None = None
    expires_at: datetime | None = None
    revoked: bool = False
    scopes: str | None = None
    last_used_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    created_at: datetime | None = None
    token_family_id: str | None = None
    rotation_counter: int = 0


@dataclass(frozen=True)
class OAuthAccountDTO:
    """Immutable OAuth account link data transfer object."""

    id: str
    user_id: str
    provider: str
    provider_user_id: str
    provider_email: str | None = None
    display_name: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ZoneDTO:
    """Immutable zone data transfer object."""

    zone_id: str
    name: str
    domain: str | None = None
    description: str | None = None
    phase: str = "Active"
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SystemSettingDTO:
    """Immutable system setting data transfer object."""

    key: str
    value: str
    description: str | None = None


@dataclass(frozen=True)
class SessionDTO:
    """Immutable user session data transfer object.

    Replaces UserSessionModel (SQLAlchemy ORM) — sessions are ephemeral KV
    with TTL, stored in CacheStore per data-storage-matrix.md Part 6.
    """

    session_id: str
    user_id: str
    agent_id: str | None = None
    zone_id: str = ROOT_ZONE_ID
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_activity: datetime | None = None
    ip_address: str | None = None
    user_agent: str | None = None

    def is_expired(self) -> bool:
        """Check if session has expired."""
        if self.expires_at is None:
            return False
        from datetime import UTC

        return datetime.now(UTC) > self.expires_at
