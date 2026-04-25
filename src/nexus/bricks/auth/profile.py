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

import builtins
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Final, Protocol, TypeAlias, cast, runtime_checkable

from nexus.bricks.auth.credential_backend import ResolvedCredential as ResolvedCredential


class _CooldownUnchangedType:
    __slots__ = ()


COOLDOWN_UNCHANGED: Final = _CooldownUnchangedType()
CooldownUpdate: TypeAlias = datetime | None | _CooldownUnchangedType


def _normalize_utc_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _merge_cooldown_state(
    *,
    existing_until: datetime | None,
    existing_reason: "AuthProfileFailureReason | None",
    requested_until: CooldownUpdate,
    requested_reason: "AuthProfileFailureReason",
    now: datetime | None = None,
) -> tuple[datetime | None, "AuthProfileFailureReason | None"]:
    normalized_now = _normalize_utc_timestamp(now) or datetime.now(UTC)
    normalized_existing = _normalize_utc_timestamp(existing_until)

    if requested_until is COOLDOWN_UNCHANGED:
        if normalized_existing is not None and normalized_existing > normalized_now:
            return normalized_existing, existing_reason
        return normalized_existing, requested_reason

    normalized_requested = _normalize_utc_timestamp(cast(datetime | None, requested_until))
    if normalized_requested is None:
        return None, requested_reason
    if (
        normalized_existing is not None
        and normalized_existing > normalized_now
        and normalized_existing >= normalized_requested
    ):
        return normalized_existing, existing_reason
    return normalized_requested, requested_reason


def _later_timestamp(left: datetime | None, right: datetime | None) -> datetime | None:
    normalized_left = _normalize_utc_timestamp(left)
    normalized_right = _normalize_utc_timestamp(right)
    if normalized_left is None:
        return normalized_right
    if normalized_right is None:
        return normalized_left
    return normalized_left if normalized_left >= normalized_right else normalized_right


def _merge_usage_stats_for_preserve(
    existing: "ProfileUsageStats",
    requested: "ProfileUsageStats",
) -> "ProfileUsageStats":
    now = datetime.now(UTC)
    existing_cooldown = _normalize_utc_timestamp(existing.cooldown_until)
    requested_cooldown = _normalize_utc_timestamp(requested.cooldown_until)
    if requested_cooldown is None:
        merged_cooldown = existing_cooldown
        merged_cooldown_reason = requested.cooldown_reason or existing.cooldown_reason
    elif (
        existing_cooldown is not None
        and existing_cooldown > now
        and existing_cooldown >= requested_cooldown
    ):
        merged_cooldown = existing_cooldown
        merged_cooldown_reason = existing.cooldown_reason
    else:
        merged_cooldown = requested_cooldown
        merged_cooldown_reason = requested.cooldown_reason

    existing_disabled = _normalize_utc_timestamp(existing.disabled_until)
    requested_disabled = _normalize_utc_timestamp(requested.disabled_until)
    if (
        requested_disabled is None
        or existing_disabled is not None
        and existing_disabled > now
        and existing_disabled >= requested_disabled
    ):
        merged_disabled = existing_disabled
    else:
        merged_disabled = requested_disabled

    return ProfileUsageStats(
        last_used_at=_later_timestamp(existing.last_used_at, requested.last_used_at),
        success_count=max(existing.success_count, requested.success_count),
        failure_count=max(existing.failure_count, requested.failure_count),
        cooldown_until=merged_cooldown,
        cooldown_reason=merged_cooldown_reason,
        disabled_until=merged_disabled,
        raw_error=requested.raw_error if requested.raw_error is not None else existing.raw_error,
    )


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
    preserve_runtime_state: bool = False


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

    def upsert(self, profile: AuthProfile, *, preserve_runtime_state: bool = False) -> None:
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
        cooldown_until: CooldownUpdate = COOLDOWN_UNCHANGED,
    ) -> None:
        """Record a failure reason and increment failure count.

        ``cooldown_until`` is optional so callers that already own a failure
        cooldown policy (for example ``CredentialPool``) can apply the counter
        update and the cooldown change atomically inside the store. When the
        argument is omitted, implementations preserve the current cooldown.
        """
        ...

    def replace_owned_subset(
        self,
        *,
        upserts: "builtins.list[AuthProfile]",
        deletes: "builtins.list[str]",
    ) -> None:
        """Atomically apply a batch of upserts then deletes.

        Used by the adapter registry to swap an owner's profile set in one
        transaction so concurrent readers never see a half-applied snapshot
        (some new rows alongside about-to-be-tombstoned rows).
        """
        ...


# ---------------------------------------------------------------------------
# CredentialCarryingProfileStore sub-protocol (issue #3803)
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialCarryingProfileStore(AuthProfileStore, Protocol):
    """Sub-protocol for stores that additionally hold encrypted credentials.

    Only ``PostgresAuthProfileStore`` implements this today. Consumers that
    need the resolved credential inline (rather than via a ``CredentialBackend``
    pointer) type-annotate against this protocol instead of the base one.

    Rows written via plain ``upsert`` are compatible: ``get_with_credential``
    returns ``(profile, None)`` in that case.
    """

    def upsert_with_credential(self, profile: AuthProfile, credential: ResolvedCredential) -> None:
        """Insert or update ``profile`` and store ``credential`` encrypted."""
        ...

    def get_with_credential(
        self, profile_id: str
    ) -> tuple[AuthProfile, ResolvedCredential | None] | None:
        """Return ``(profile, credential | None)`` or ``None`` if absent.

        ``credential`` is ``None`` when the row has no ciphertext columns (PR 1
        routing-only rows).
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

    def upsert(self, profile: AuthProfile, *, preserve_runtime_state: bool = False) -> None:
        if not preserve_runtime_state:
            self._profiles[profile.id] = profile
            return
        existing = self._profiles.get(profile.id)
        if existing is None:
            self._profiles[profile.id] = profile
            return
        merged_stats = _merge_usage_stats_for_preserve(
            existing.usage_stats,
            profile.usage_stats,
        )
        self._profiles[profile.id] = AuthProfile(
            id=profile.id,
            provider=profile.provider,
            account_identifier=profile.account_identifier,
            backend=profile.backend,
            backend_key=profile.backend_key,
            last_synced_at=profile.last_synced_at,
            sync_ttl_seconds=profile.sync_ttl_seconds,
            usage_stats=merged_stats,
            preserve_runtime_state=profile.preserve_runtime_state,
        )

    def delete(self, profile_id: str) -> None:
        self._profiles.pop(profile_id, None)

    def mark_success(self, profile_id: str) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        stats = profile.usage_stats
        stats.success_count += 1
        stats.last_used_at = datetime.now(UTC)
        now = datetime.now(UTC)
        cooldown_until = _normalize_utc_timestamp(stats.cooldown_until)
        if cooldown_until is None or cooldown_until <= now:
            stats.cooldown_until = None
            stats.cooldown_reason = None
        else:
            stats.cooldown_until = cooldown_until

    def mark_failure(
        self,
        profile_id: str,
        reason: AuthProfileFailureReason,
        *,
        raw_error: str | None = None,
        cooldown_until: CooldownUpdate = COOLDOWN_UNCHANGED,
    ) -> None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return
        stats = profile.usage_stats
        stats.failure_count += 1
        stats.last_used_at = datetime.now(UTC)
        stats.cooldown_until, stats.cooldown_reason = _merge_cooldown_state(
            existing_until=stats.cooldown_until,
            existing_reason=stats.cooldown_reason,
            requested_until=cooldown_until,
            requested_reason=reason,
            now=stats.last_used_at,
        )
        if raw_error is not None:
            stats.raw_error = raw_error[:RAW_ERROR_MAX_LEN]

    def replace_owned_subset(
        self,
        *,
        upserts: "builtins.list[AuthProfile]",
        deletes: "builtins.list[str]",
    ) -> None:
        # Single-threaded by contract — applies as a single visible step.
        for p in upserts:
            self._profiles[p.id] = p
        for pid in deletes:
            self._profiles.pop(pid, None)
