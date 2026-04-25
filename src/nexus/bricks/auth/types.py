"""Auth brick types — core data structures.

AuthResult is the primary output of authentication, consumed by
the server layer (dependencies.py) and downstream services.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AuthResult:
    """Immutable result of an authentication attempt.

    The subject_type + subject_id tuple forms the ReBAC subject identity.
    zone_id is metadata only — it does not define identity.
    zone_set is the full zone allow-list for the token (#3785); empty tuple
    means unconstrained (e.g. admin/internal keys).

    Examples:
        AuthResult(True, "user", "alice", "org_acme", False)
        AuthResult(True, "agent", "agent_123", "org_acme", False)
        AuthResult(True, "service", "backup_bot", None, True)
    """

    authenticated: bool
    subject_type: str = "user"
    subject_id: str | None = None
    zone_id: str | None = None
    is_admin: bool = False
    metadata: dict[str, Any] | None = None
    agent_generation: int | None = None
    inherit_permissions: bool = True
    zone_set: tuple[str, ...] = ()  # #3785: full zone allow-list for this token


@dataclass(frozen=True)
class UserInfo:
    """Immutable user information DTO.

    Decouples auth brick code from SQLAlchemy's UserModel ORM class.
    Protocols return UserInfo instead of UserModel so the auth brick
    has zero dependency on nexus.storage.models.

    Issue #2281: Extract Auth/OAuth brick from server/auth.
    """

    user_id: str
    email: str | None = None
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    password_hash: str | None = None
    primary_auth_method: str | None = None
    is_global_admin: bool = False
    is_active: bool = True
    email_verified: bool = False
    zone_id: str | None = None
    api_key: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuthConfig:
    """Configuration for the auth brick.

    Attributes:
        auth_type: Authentication strategy
        cache_ttl_seconds: Auth result cache TTL
        cache_max_size: Maximum cache entries
        require_expiry: Reject API keys without expiry
        auth_config: Provider-specific configuration dict
    """

    auth_type: str | None = None
    cache_ttl_seconds: int = 900
    cache_max_size: int = 1000
    require_expiry: bool = False
    auth_config: dict[str, Any] = field(default_factory=dict)
