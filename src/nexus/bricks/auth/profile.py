"""Auth profile data model and store protocol.

This module defines:
  - AuthProfileFailureReason: enum mapping every provider's failure vocabulary
    to a single classification (ported from OpenClaw's AuthProfileFailureReason).
  - AuthProfile / ProfileUsageStats: unified routing metadata for credentials.
  - ResolvedCredential: the output of a backend resolve() call (re-exported
    from credential_backend.py for convenience).
  - AuthProfileStore: Protocol for the unified auth-profile store. Concrete
    implementations: SqliteAuthProfileStore (#3738), InMemoryAuthProfileStore.
  - InMemoryAuthProfileStore: dict-backed stub for tests.

Architecture (epic #3722, decision 1A):
  AuthProfile is *routing metadata only*. It holds identity (id, provider,
  account_identifier), a pointer to the credential backend (backend,
  backend_key), sync bookkeeping, and usage stats. The actual credential
  lives inside a pluggable CredentialBackend implementation — see
  credential_backend.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from nexus.bricks.auth.credential_backend import ResolvedCredential as ResolvedCredential

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
    SESSION_EXPIRED = "session_expired"  # requires user re-authentication
    MFA_REQUIRED = "mfa_required"  # multi-factor challenge pending
    PROXY_OR_TLS = "proxy_or_tls"  # TLS handshake / proxy misconfiguration
    UPSTREAM_CLI_MISSING = "upstream_cli_missing"  # aws/gcloud/gh binary not found
    SCOPE_INSUFFICIENT = "scope_insufficient"  # token lacks required OAuth scopes
    CLOCK_SKEW = "clock_skew"  # client/server clock drift broke signature validation
    UNKNOWN = "unknown"  # fallback when classifier can't map the error

    # Deprecated: remove in Phase 4 (#3741). Kept for backward compatibility
    # with existing classifiers and cooldown policy entries.
    MODEL_NOT_FOUND = "model_not_found"


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
    # Escape hatch: raw error string from the provider for debugging UNKNOWN
    # failures. Truncated to _RAW_ERROR_MAX_LEN on store write.
    raw_error: str | None = None


# Maximum length for raw_error persisted to the store. The dataclass itself
# accepts any length — truncation is enforced at the store write path.
RAW_ERROR_MAX_LEN = 500


# ---------------------------------------------------------------------------
# Auth profile (decision 1A: routing metadata only)
# ---------------------------------------------------------------------------


@dataclass
class AuthProfile:
    """Unified routing record for a single credential.

    Does NOT hold the credential itself. The ``backend`` + ``backend_key``
    pair identifies which CredentialBackend can resolve the actual secret.
    """

    id: str  # e.g. "google/user@example.com"
    provider: str  # e.g. "google", "openai", "aws"
    account_identifier: str  # e.g. "user@example.com"
    backend: str  # e.g. "nexus-token-manager", "aws-cli", "gcloud"
    backend_key: str  # opaque key the backend uses to resolve the credential
    last_synced_at: datetime | None = None
    sync_ttl_seconds: int = 300
    usage_stats: ProfileUsageStats = field(default_factory=ProfileUsageStats)


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuthProfileStore(Protocol):
    """Protocol for the unified auth-profile store.

    Implementations: SqliteAuthProfileStore (profile_store.py),
    InMemoryAuthProfileStore (below).
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

    def mark_success(self, profile_id: str) -> None:
        """Record a successful credential use for the given profile."""
        ...

    def mark_failure(
        self,
        profile_id: str,
        reason: AuthProfileFailureReason,
        *,
        raw_error: str | None = None,
    ) -> None:
        """Record a failure reason and increment failure count.

        Does NOT set cooldown_until — cooldown duration policy is owned by
        CredentialPool, which calls store.upsert() after computing the
        cooldown. This method only persists the failure classification.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory store — tests and pre-SQLite fallback
# ---------------------------------------------------------------------------


class InMemoryAuthProfileStore:
    """Dict-backed AuthProfileStore for tests.

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

    def mark_success(self, profile_id: str) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        stats = profile.usage_stats
        stats.success_count += 1
        stats.last_used_at = datetime.utcnow()

    def mark_failure(
        self,
        profile_id: str,
        reason: AuthProfileFailureReason,
        *,
        raw_error: str | None = None,
    ) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        stats = profile.usage_stats
        stats.failure_count += 1
        stats.last_used_at = datetime.utcnow()
        stats.cooldown_reason = reason
        if raw_error is not None:
            stats.raw_error = raw_error[:RAW_ERROR_MAX_LEN]
