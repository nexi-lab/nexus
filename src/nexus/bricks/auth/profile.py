"""Auth profile data model and store protocol.

This module defines:
  - AuthProfileFailureReason: enum mapping every provider's failure vocabulary
    to a single classification (ported from OpenClaw's AuthProfileFailureReason).
  - AuthProfile / ProfileUsageStats: runtime credential data model.
  - AuthProfileStore: Protocol that #3722 implements with SQLite. All code in
    #3723 depends only on this Protocol — never on the concrete implementation.
  - InMemoryAuthProfileStore: dict-backed stub for tests and pre-#3722 use.

Issue #3722 will land SqliteAuthProfileStore implementing AuthProfileStore.
This issue (#3723) uses only the Protocol so it can land independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Credential shapes
# ---------------------------------------------------------------------------


@dataclass
class ApiKeyCredential:
    kind: Literal["api_key"] = "api_key"
    key: str = ""


@dataclass
class TokenCredential:
    kind: Literal["token"] = "token"
    access_token: str = ""
    refresh_token: str | None = None
    expires_at: datetime | None = None


@dataclass
class OAuthCredential:
    kind: Literal["oauth"] = "oauth"
    access_token: str = ""
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)


AuthProfileCredential = ApiKeyCredential | TokenCredential | OAuthCredential

ExternalCliManager = Literal["aws-cli", "gcloud", "gh-cli", "gws-cli", "codex-cli"]


# ---------------------------------------------------------------------------
# Failure reason enum (OpenClaw pattern)
# ---------------------------------------------------------------------------


class AuthProfileFailureReason(Enum):
    AUTH = "auth"  # wrong credentials (wrong password, typo)
    AUTH_PERMANENT = "auth_permanent"  # revoked — requires user action to recover
    FORMAT = "format"  # malformed token (not an auth problem per se)
    OVERLOADED = "overloaded"  # provider temporary issue, try again soon
    RATE_LIMIT = "rate_limit"  # 429 — cooldown + auto-recover
    BILLING = "billing"  # 402 / insufficient_quota — long cooldown
    TIMEOUT = "timeout"  # network issue, retry immediately
    MODEL_NOT_FOUND = "model_not_found"  # not applicable to all providers
    SESSION_EXPIRED = "session_expired"  # requires user re-authentication
    UNKNOWN = "unknown"  # fallback when classifier can't map the error


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------


@dataclass
class ProfileUsageStats:
    last_used_at: datetime | None = None
    success_count: int = 0
    failure_count: int = 0
    cooldown_until: datetime | None = None
    cooldown_reason: AuthProfileFailureReason | None = None
    # disabled_until is for operator-set disables (billing review, manual ban).
    # cooldown_until is for automatic cooldowns after failures.
    # Both are checked by _is_usable; either blocks selection.
    disabled_until: datetime | None = None


# ---------------------------------------------------------------------------
# Auth profile
# ---------------------------------------------------------------------------


@dataclass
class AuthProfile:
    id: str  # e.g. "openai/default"
    provider: str  # e.g. "openai"
    account_identifier: str  # e.g. "default" or "user@example.com"
    credential: AuthProfileCredential
    managed_by: ExternalCliManager | None = None  # None = nexus-native
    last_synced_at: datetime | None = None
    sync_ttl_seconds: int = 300
    usage_stats: ProfileUsageStats = field(default_factory=ProfileUsageStats)


# ---------------------------------------------------------------------------
# Store protocol — #3722 provides SqliteAuthProfileStore.
# ---------------------------------------------------------------------------


@runtime_checkable
class AuthProfileStore(Protocol):
    """Protocol for the unified auth-profile store.

    Issue #3723 depends only on this Protocol.
    Issue #3722 provides the concrete SqliteAuthProfileStore.
    Tests use InMemoryAuthProfileStore so #3723 can land without #3722.
    """

    def list(self, *, provider: str | None = None) -> list[AuthProfile]:
        """Return all profiles, optionally filtered by provider."""
        ...

    def get(self, profile_id: str) -> AuthProfile | None:
        """Return one profile by ID, or None if not found."""
        ...

    def upsert(self, profile: AuthProfile) -> None:
        """Insert or update a profile (keyed by profile.id)."""
        ...

    def delete(self, profile_id: str) -> None:
        """Remove a profile by ID."""
        ...


# ---------------------------------------------------------------------------
# In-memory store — tests and pre-#3722 fallback
# ---------------------------------------------------------------------------


class InMemoryAuthProfileStore:
    """Dict-backed AuthProfileStore for tests and local use before #3722 lands.

    Not thread-safe. Wrap with asyncio.Lock if used across concurrent tasks.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, AuthProfile] = {}

    def list(self, *, provider: str | None = None) -> list[AuthProfile]:
        if provider is None:
            return list(self._profiles.values())
        return [p for p in self._profiles.values() if p.provider == provider]

    def get(self, profile_id: str) -> AuthProfile | None:
        return self._profiles.get(profile_id)

    def upsert(self, profile: AuthProfile) -> None:
        self._profiles[profile.id] = profile

    def delete(self, profile_id: str) -> None:
        self._profiles.pop(profile_id, None)
