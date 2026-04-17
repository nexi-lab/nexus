"""Credential pool with multi-account failover and cooldown-based rotation.

Ported from the Hermes Agent pattern (agent/credential_pool.py) with the
OpenClaw failure-reason enum (src/agents/auth-profiles/types.ts).

## Responsibility boundary

This module handles *credential selection and switching*. It does NOT handle
same-credential retries — use ``tenacity`` for those:

  - tenacity: retry the same credential on transient failures (network blips,
    5xx without a better alternative). Example: ``@retry(stop=stop_after_attempt(3))``.
  - pool.execute(): switch to a different credential on RATE_LIMIT / OVERLOADED /
    TIMEOUT, after marking the failing profile on cooldown.

Using both together is correct and expected. Never add tenacity retries inside
pool.execute(); never add credential-switching inside tenacity callbacks.

## Usage

    from nexus.bricks.auth.credential_pool import CredentialPool, CredentialPoolRegistry
    from nexus.bricks.auth.classifiers.openai import classify_openai_error

    # At application startup (once, process-scoped):
    registry = CredentialPoolRegistry(store=profile_store)

    # In a connector:
    pool = registry.get("openai", strategy="least_used")
    result = await pool.execute(
        lambda profile: openai_client.chat(
            token=resolve_credential(profile.backend_key)
        ),
        classifier=classify_openai_error,
    )
"""

from __future__ import annotations

import inspect
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileFailureReason,
    AuthProfileStore,
    ProfileUsageStats,
)

logger = logging.getLogger(__name__)

SelectionStrategy = Literal["first_ok", "round_robin", "random", "least_used"]

# Failures that trigger a single automatic retry with a different credential.
_RETRIABLE_REASONS: frozenset[AuthProfileFailureReason] = frozenset(
    {
        AuthProfileFailureReason.RATE_LIMIT,
        AuthProfileFailureReason.OVERLOADED,
        AuthProfileFailureReason.TIMEOUT,
    }
)

# Default cooldown durations per failure reason.
# Override per-pool via the cooldown_overrides constructor argument.
_DEFAULT_COOLDOWN_POLICY: dict[AuthProfileFailureReason, timedelta | None] = {
    AuthProfileFailureReason.RATE_LIMIT: timedelta(hours=1),
    AuthProfileFailureReason.OVERLOADED: timedelta(minutes=5),
    AuthProfileFailureReason.TIMEOUT: timedelta(seconds=30),
    AuthProfileFailureReason.BILLING: timedelta(hours=24),
    AuthProfileFailureReason.SESSION_EXPIRED: timedelta(days=365),
    AuthProfileFailureReason.AUTH_PERMANENT: timedelta(days=365),
    AuthProfileFailureReason.AUTH: None,
    AuthProfileFailureReason.FORMAT: None,
    AuthProfileFailureReason.MFA_REQUIRED: timedelta(days=365),
    AuthProfileFailureReason.PROXY_OR_TLS: timedelta(minutes=5),
    AuthProfileFailureReason.UPSTREAM_CLI_MISSING: None,  # structural — needs user fix
    AuthProfileFailureReason.SCOPE_INSUFFICIENT: None,  # structural — needs re-auth
    AuthProfileFailureReason.CLOCK_SKEW: timedelta(minutes=5),
    AuthProfileFailureReason.UNKNOWN: timedelta(minutes=1),
}


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExhaustedProfile:
    """One profile's state at the point of exhaustion, for structured error reporting."""

    profile: AuthProfile
    reason: AuthProfileFailureReason | None
    cooldown_eta: datetime | None


class NoAvailableCredentialError(Exception):
    """Raised when all profiles for a provider are on cooldown or disabled.

    Carries structured per-profile state so callers (CLI, connectors) can
    produce actionable error messages without re-querying the store.

    Attributes:
        provider: The provider name (e.g. "openai").
        exhausted_profiles: One entry per profile, with its reason and ETA.
    """

    def __init__(
        self,
        provider: str,
        exhausted_profiles: list[ExhaustedProfile],
    ) -> None:
        self.provider = provider
        self.exhausted_profiles = exhausted_profiles

        lines: list[str] = []
        for ep in exhausted_profiles:
            if ep.cooldown_eta:
                eta_str = ep.cooldown_eta.strftime("%Y-%m-%dT%H:%M:%SZ")
                reason_str = ep.reason.value if ep.reason else "unknown"
                lines.append(f"  {ep.profile.account_identifier}: {reason_str} until {eta_str}")
            else:
                reason_str = ep.reason.value if ep.reason else "disabled"
                lines.append(f"  {ep.profile.account_identifier}: {reason_str}")

        detail_block = "\n".join(lines) if lines else "  (no profiles configured)"
        super().__init__(
            f"No available credential for provider '{provider}':\n"
            f"{detail_block}\n"
            f"Check AuthProfileStore for current cooldown state, or wait for "
            f"cooldowns to expire before retrying."
        )


# ---------------------------------------------------------------------------
# Classifier protocol
# ---------------------------------------------------------------------------


class CredentialErrorClassifier(Protocol):
    """Maps a provider exception to an AuthProfileFailureReason.

    One classifier per provider SDK (openai, anthropic, google, etc.).
    See nexus.bricks.auth.classifiers for implementations.
    """

    def __call__(self, __exc: Exception) -> AuthProfileFailureReason: ...


# ---------------------------------------------------------------------------
# CredentialPool
# ---------------------------------------------------------------------------


class CredentialPool:
    """Runtime-managed pool of credentials for one provider.

    The pool is a *view* over AuthProfileStore — it does not store credentials
    itself; it implements selection + failure-handling policy.

    Thread/async safety: _last_index (round_robin) is protected by threading.Lock.
    All other operations are safe under concurrent async access.

    Performance: when the store's LRU cache is enabled (SqliteAuthProfileStore),
    store.list() is a cache read — no SQLite I/O on the hot path.
    """

    def __init__(
        self,
        provider: str,
        store: AuthProfileStore,
        *,
        strategy: SelectionStrategy = "first_ok",
        cooldown_overrides: dict[AuthProfileFailureReason, timedelta | None] | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.strategy: SelectionStrategy = strategy
        # round_robin increments before reading; -1 ensures first call returns index 0
        self._last_index: int = -1
        # threading.Lock (not asyncio.Lock) so the same lock protects both
        # async select() callers and sync select_sync() callers from thread
        # executors (e.g. CASOpenAIBackend.generate_streaming runs in a thread).
        # The critical section is nanoseconds — no meaningful event-loop blocking.
        self._lock = threading.Lock()
        self._cooldown_policy: dict[AuthProfileFailureReason, timedelta | None] = {
            **_DEFAULT_COOLDOWN_POLICY,
            **(cooldown_overrides or {}),
        }

    # ------------------------------------------------------------------
    # Selection (shared implementation)
    # ------------------------------------------------------------------

    def _select_impl(
        self,
        *,
        account_identifier: str | None = None,
    ) -> AuthProfile:
        """Core selection logic shared by select() and select_sync().

        Freeze ``now`` once so every _is_usable comparison uses the same instant.
        This prevents off-by-one races at exact cooldown boundaries and makes
        parametrized boundary tests deterministic.
        """
        now = datetime.utcnow()

        all_profiles = self.store.list(provider=self.provider)
        if account_identifier is not None:
            all_profiles = [p for p in all_profiles if p.account_identifier == account_identifier]

        candidates = [p for p in all_profiles if self._is_usable(p, now)]

        if not candidates:
            exhausted = [
                ExhaustedProfile(
                    profile=p,
                    reason=p.usage_stats.cooldown_reason,
                    cooldown_eta=(p.usage_stats.cooldown_until or p.usage_stats.disabled_until),
                )
                for p in all_profiles
            ]
            raise NoAvailableCredentialError(
                provider=self.provider,
                exhausted_profiles=exhausted,
            )

        match self.strategy:
            case "first_ok":
                return candidates[0]
            case "round_robin":
                with self._lock:
                    self._last_index = (self._last_index + 1) % len(candidates)
                    idx = self._last_index
                return candidates[idx]
            case "random":
                return random.choice(candidates)
            case "least_used":
                return min(
                    candidates,
                    key=lambda p: p.usage_stats.success_count + p.usage_stats.failure_count,
                )
            case _:
                raise ValueError(f"Unknown strategy: {self.strategy!r}")

    async def select(
        self,
        *,
        account_identifier: str | None = None,
    ) -> AuthProfile:
        """Return a usable profile per this pool's strategy (async).

        Skips profiles on cooldown or operator-disabled. Raises
        NoAvailableCredentialError if all profiles are unavailable.
        """
        return self._select_impl(account_identifier=account_identifier)

    def select_sync(
        self,
        *,
        account_identifier: str | None = None,
    ) -> AuthProfile:
        """Return a usable profile per this pool's strategy (sync).

        Semantically identical to select(). Use from sync code
        (generator functions, thread-pool executors).
        """
        return self._select_impl(account_identifier=account_identifier)

    # ------------------------------------------------------------------
    # Outcome recording
    # ------------------------------------------------------------------

    def mark_success(self, profile: AuthProfile) -> None:
        """Record a successful use; clear cooldown only if no newer failure exists.

        Under concurrent load a later request may already have recorded a
        RATE_LIMIT/OVERLOADED failure (setting cooldown_until in the future)
        before an earlier request's success is recorded.  Unconditionally
        clearing cooldown_until here would allow a throttled credential back
        into rotation immediately, causing repeated hammering.

        Safe rule: only clear the cooldown if it has already passed (or was
        never set), meaning no newer failure has imposed a future cooldown.
        """
        stats = profile.usage_stats
        stats.success_count += 1
        stats.last_used_at = datetime.utcnow()
        now = datetime.utcnow()
        if stats.cooldown_until is None or stats.cooldown_until <= now:
            stats.cooldown_until = None
            stats.cooldown_reason = None
        self.store.upsert(profile)

    def mark_failure(
        self,
        profile: AuthProfile,
        reason: AuthProfileFailureReason,
    ) -> None:
        """Record a failure and apply the policy cooldown for this reason."""
        stats = profile.usage_stats
        stats.failure_count += 1
        stats.last_used_at = datetime.utcnow()
        cooldown = self._cooldown_policy.get(reason)
        if cooldown is not None:
            stats.cooldown_until = datetime.utcnow() + cooldown
        stats.cooldown_reason = reason
        self.store.upsert(profile)
        logger.warning(
            "Credential failure: provider=%s account=%s reason=%s cooldown=%s",
            self.provider,
            profile.account_identifier,
            reason.value,
            cooldown,
        )

    # ------------------------------------------------------------------
    # Combined execute with single credential-switch retry (shared impl)
    # ------------------------------------------------------------------

    def _execute_attempt(
        self,
        fn: Callable[[AuthProfile], Any],
        profile: AuthProfile,
        classifier: CredentialErrorClassifier,
        bypass_exceptions: tuple[type[Exception], ...],
        *,
        is_retry: bool = False,
    ) -> tuple[bool, Any]:
        """Run fn with profile, handle success/failure classification.

        Returns (success, result_or_none). On retriable failure during the
        first attempt, returns (False, None) to signal the caller to retry.
        On non-retriable failure or retry failure, re-raises.
        """
        try:
            result = fn(profile)
            return True, result
        except Exception as exc:
            if bypass_exceptions and isinstance(exc, bypass_exceptions):
                raise
            try:
                reason = classifier(exc)
            except Exception:
                reason = AuthProfileFailureReason.UNKNOWN
            self.mark_failure(profile, reason)
            if is_retry or reason not in _RETRIABLE_REASONS:
                raise exc
            return False, None

    async def _execute_attempt_async(
        self,
        fn: Callable[[AuthProfile], Any],
        profile: AuthProfile,
        classifier: CredentialErrorClassifier,
        bypass_exceptions: tuple[type[Exception], ...],
        *,
        is_retry: bool = False,
    ) -> tuple[bool, Any]:
        """Async version of _execute_attempt — awaits fn if it returns awaitable."""
        try:
            result = fn(profile)
            if inspect.isawaitable(result):
                result = await result
            return True, result
        except Exception as exc:
            if bypass_exceptions and isinstance(exc, bypass_exceptions):
                raise
            try:
                reason = classifier(exc)
            except Exception:
                reason = AuthProfileFailureReason.UNKNOWN
            self.mark_failure(profile, reason)
            if is_retry or reason not in _RETRIABLE_REASONS:
                raise exc
            return False, None

    def execute_sync(
        self,
        fn: Callable[[AuthProfile], Any],
        classifier: CredentialErrorClassifier,
        *,
        account_identifier: str | None = None,
        bypass_exceptions: tuple[type[Exception], ...] = (),
    ) -> Any:
        """Select a credential, call fn, handle failure, retry on retriable errors (sync).

        Args:
            fn: Callable accepting an AuthProfile, returning T.
            classifier: Maps provider exceptions to AuthProfileFailureReason.
            account_identifier: Passed through to select_sync() for scoped pools.
            bypass_exceptions: Exception types that are NOT credential failures.

        Returns:
            Whatever fn returns.

        Raises:
            NoAvailableCredentialError: if all profiles exhausted.
            Exception: non-retriable failure from fn, re-raised after mark_failure.
        """
        profile = self.select_sync(account_identifier=account_identifier)
        ok, result = self._execute_attempt(
            fn,
            profile,
            classifier,
            bypass_exceptions,
        )
        if ok:
            self.mark_success(profile)
            return result

        # Single retry with a different credential (first is now on cooldown)
        next_profile = self.select_sync(account_identifier=account_identifier)
        ok, result = self._execute_attempt(
            fn,
            next_profile,
            classifier,
            bypass_exceptions,
            is_retry=True,
        )
        self.mark_success(next_profile)
        return result

    async def execute(
        self,
        fn: Callable[[AuthProfile], Any],
        classifier: CredentialErrorClassifier,
        *,
        account_identifier: str | None = None,
        bypass_exceptions: tuple[type[Exception], ...] = (),
    ) -> Any:
        """Select a credential, call fn, handle failure, retry on retriable errors (async).

        Args:
            fn: Callable accepting an AuthProfile, returning T or Awaitable[T].
            classifier: Maps the provider's exception to AuthProfileFailureReason.
            account_identifier: Passed through to select() for user-scoped pools.
            bypass_exceptions: Exception types that are NOT credential failures.

        Returns:
            Whatever fn returns (awaited if it is a coroutine).

        Raises:
            NoAvailableCredentialError: all profiles exhausted before or after retry.
            Exception: non-retriable failure from fn, re-raised after mark_failure.
        """
        profile = await self.select(account_identifier=account_identifier)
        ok, result = await self._execute_attempt_async(
            fn,
            profile,
            classifier,
            bypass_exceptions,
        )
        if ok:
            self.mark_success(profile)
            return result

        # Single retry with a different credential (first profile is now on cooldown).
        next_profile = await self.select(account_identifier=account_identifier)
        ok, result = await self._execute_attempt_async(
            fn,
            next_profile,
            classifier,
            bypass_exceptions,
            is_retry=True,
        )
        self.mark_success(next_profile)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_usable(profile: AuthProfile, now: datetime) -> bool:
        """Return True if the profile is available for selection right now.

        Both disabled_until (operator-set) and cooldown_until (auto-set) block
        selection independently. A profile is usable when neither is in the future.
        """
        stats: ProfileUsageStats = profile.usage_stats
        if stats.disabled_until is not None and stats.disabled_until > now:
            return False
        return not (stats.cooldown_until is not None and stats.cooldown_until > now)


# ---------------------------------------------------------------------------
# CredentialPoolRegistry
# ---------------------------------------------------------------------------


class CredentialPoolRegistry:
    """Process-scoped registry — one CredentialPool per provider.

    Instantiate once at application startup alongside the auth brick and pass
    to connectors as a dependency. This ensures round_robin and least_used
    strategies are fairly distributed across all callers and requests.

    Usage:
        registry = CredentialPoolRegistry(store=profile_store)
        pool = registry.get("openai", strategy="least_used")
    """

    def __init__(self, store: AuthProfileStore) -> None:
        self.store = store
        self._pools: dict[str, CredentialPool] = {}

    def get(
        self,
        provider: str,
        *,
        strategy: SelectionStrategy = "first_ok",
        cooldown_overrides: dict[AuthProfileFailureReason, timedelta | None] | None = None,
    ) -> CredentialPool:
        """Return the pool for a provider, creating it on first access.

        The strategy and cooldown_overrides are only applied at pool creation.
        If the pool already exists, the existing configuration is returned as-is.
        """
        if provider not in self._pools:
            self._pools[provider] = CredentialPool(
                provider=provider,
                store=self.store,
                strategy=strategy,
                cooldown_overrides=cooldown_overrides,
            )
        return self._pools[provider]

    def shutdown(self) -> None:
        """Clear all pools on application shutdown."""
        self._pools.clear()
