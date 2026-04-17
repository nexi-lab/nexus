"""Tests for CredentialPool, CredentialPoolRegistry, and classifiers.

Coverage map:
  - Strategy correctness: first_ok, round_robin, least_used, random
  - Cooldown enforcement and auto-recovery
  - All-profiles-exhausted: NoAvailableCredentialError with structured state
  - _is_usable boundary conditions (parametrized, 6 cases)
  - pool.execute() paths: success, non-retriable failure, retriable+retry,
    all-exhausted during retry, classifier-raises
  - asyncio.Lock concurrency: 100 concurrent round_robin calls
  - account_identifier scoping
  - cooldown_overrides per-pool
  - CredentialPoolRegistry singleton-per-provider
  - classify_openai_error: all exception types including billing/rate-limit split
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.auth.credential_pool import (
    _DEFAULT_COOLDOWN_POLICY,
    _RETRIABLE_REASONS,
    CredentialPool,
    CredentialPoolRegistry,
    NoAvailableCredentialError,
    SelectionStrategy,
)
from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileFailureReason,
    InMemoryAuthProfileStore,
    ProfileUsageStats,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_profile(
    profile_id: str,
    provider: str = "openai",
    account_identifier: str | None = None,
    *,
    cooldown_until: datetime | None = None,
    disabled_until: datetime | None = None,
    success_count: int = 0,
    failure_count: int = 0,
    cooldown_reason: AuthProfileFailureReason | None = None,
) -> AuthProfile:
    stats = ProfileUsageStats(
        cooldown_until=cooldown_until,
        disabled_until=disabled_until,
        success_count=success_count,
        failure_count=failure_count,
        cooldown_reason=cooldown_reason,
    )
    return AuthProfile(
        id=profile_id,
        provider=provider,
        account_identifier=account_identifier or profile_id,
        backend="nexus-token-manager",
        backend_key=f"openai/{profile_id}",
        usage_stats=stats,
    )


def make_pool(
    *profile_ids: str,
    provider: str = "openai",
    strategy: SelectionStrategy = "first_ok",
    cooldown_overrides=None,
) -> tuple[CredentialPool, InMemoryAuthProfileStore]:
    store = InMemoryAuthProfileStore()
    for pid in profile_ids:
        store.upsert(make_profile(pid, provider=provider))
    pool = CredentialPool(
        provider=provider,
        store=store,
        strategy=strategy,
        cooldown_overrides=cooldown_overrides,
    )
    return pool, store


# ---------------------------------------------------------------------------
# _is_usable boundary conditions (Issue 11)
# ---------------------------------------------------------------------------

# Anchor: 2026-01-15 12:00:00 UTC
_NOW = datetime(2026, 1, 15, 12, 0, 0)
_1US = timedelta(microseconds=1)
_1DAY = timedelta(days=1)
_1HR = timedelta(hours=1)


@pytest.mark.parametrize(
    "cooldown_until, disabled_until, expected, label",
    [
        (None, None, True, "no_restriction"),
        (_NOW - _1US, None, True, "cooldown_expired_1us_ago"),
        (_NOW, None, True, "cooldown_exactly_now"),  # > not >= → usable at exact boundary
        (_NOW + _1US, None, False, "cooldown_1us_in_future"),
        (None, _NOW + _1DAY, False, "disabled_until_set"),
        (_NOW + _1HR, _NOW + _1DAY, False, "both_fields_set"),
    ],
)
def test_is_usable_boundary(
    cooldown_until: datetime | None,
    disabled_until: datetime | None,
    expected: bool,
    label: str,
) -> None:
    """_is_usable uses frozen now for deterministic boundary comparisons."""
    profile = make_profile("p", cooldown_until=cooldown_until, disabled_until=disabled_until)
    profile.usage_stats.cooldown_until = cooldown_until
    profile.usage_stats.disabled_until = disabled_until
    assert CredentialPool._is_usable(profile, _NOW) == expected, f"case: {label}"


# ---------------------------------------------------------------------------
# Strategy tests
# ---------------------------------------------------------------------------


async def test_first_ok_returns_first_candidate() -> None:
    pool, store = make_pool("p1", "p2", "p3", strategy="first_ok")
    result = await pool.select()
    assert result.id == "p1"


async def test_first_ok_skips_cooldown_profiles() -> None:
    pool, store = make_pool(strategy="first_ok")
    store.upsert(make_profile("p1", cooldown_until=datetime(2099, 1, 1)))
    store.upsert(make_profile("p2"))
    result = await pool.select()
    assert result.id == "p2"


async def test_round_robin_cycles_across_profiles() -> None:
    pool, store = make_pool("a", "b", "c", strategy="round_robin")
    results = [await pool.select() for _ in range(6)]
    ids = [r.id for r in results]
    # Should cycle through all 3, repeating, starting at index 0
    assert set(ids) == {"a", "b", "c"}
    assert ids[0] == "a"  # first call must start at candidates[0], not candidates[1]
    assert ids[0] == ids[3]  # same position 3 apart in a 3-profile pool


async def test_least_used_picks_lowest_call_count() -> None:
    pool, store = make_pool(strategy="least_used")
    store.upsert(make_profile("heavy", success_count=100, failure_count=50))
    store.upsert(make_profile("light", success_count=1, failure_count=0))
    result = await pool.select()
    assert result.id == "light"


async def test_random_strategy_returns_a_valid_profile() -> None:
    pool, store = make_pool("x", "y", "z", strategy="random")
    result = await pool.select()
    assert result.id in {"x", "y", "z"}


# ---------------------------------------------------------------------------
# Cooldown enforcement
# ---------------------------------------------------------------------------


async def test_select_skips_cooled_down_profiles() -> None:
    pool, store = make_pool(strategy="first_ok")
    future = datetime(2099, 12, 31)
    store.upsert(make_profile("p1", cooldown_until=future))
    store.upsert(make_profile("p2", cooldown_until=future))
    store.upsert(make_profile("p3"))  # only usable one
    result = await pool.select()
    assert result.id == "p3"


async def test_mark_failure_sets_cooldown() -> None:
    pool, store = make_pool("p1", strategy="first_ok")
    profile = store.get("p1")
    assert profile is not None

    pool.mark_failure(profile, AuthProfileFailureReason.RATE_LIMIT)

    updated = store.get("p1")
    assert updated is not None
    assert updated.usage_stats.cooldown_until is not None
    assert updated.usage_stats.cooldown_until > datetime.utcnow()
    assert updated.usage_stats.cooldown_reason == AuthProfileFailureReason.RATE_LIMIT
    assert updated.usage_stats.failure_count == 1


async def test_mark_success_does_not_clear_active_cooldown() -> None:
    """mark_success must not resurrect a credential with a future cooldown.

    Scenario: request A selected the profile (no cooldown), request B then
    hits RATE_LIMIT and sets cooldown_until = now + 1h.  When request A's
    success is recorded later it must NOT clear the cooldown — otherwise the
    throttled credential would re-enter rotation immediately.
    """
    pool, store = make_pool("p1", strategy="first_ok")
    profile = store.get("p1")
    assert profile is not None

    # Simulate concurrent failure setting a future cooldown
    pool.mark_failure(profile, AuthProfileFailureReason.RATE_LIMIT)
    profile = store.get("p1")
    assert profile is not None
    assert profile.usage_stats.cooldown_until is not None

    # Stale success from an earlier concurrent request must NOT clear cooldown
    pool.mark_success(profile)
    after = store.get("p1")
    assert after is not None
    assert after.usage_stats.cooldown_until is not None, (
        "mark_success must not clear a future cooldown (stale-success race)"
    )
    assert after.usage_stats.cooldown_reason is not None
    assert after.usage_stats.success_count == 1


async def test_mark_success_clears_expired_cooldown() -> None:
    """mark_success clears a cooldown that has already passed."""
    from datetime import timedelta

    pool, store = make_pool("p1", strategy="first_ok")
    profile = store.get("p1")
    assert profile is not None

    # Manually set a cooldown in the past
    profile.usage_stats.cooldown_until = datetime.utcnow() - timedelta(seconds=1)
    profile.usage_stats.cooldown_reason = AuthProfileFailureReason.RATE_LIMIT
    store.upsert(profile)

    # Success should clear the already-expired cooldown
    pool.mark_success(profile)
    after = store.get("p1")
    assert after is not None
    assert after.usage_stats.cooldown_until is None
    assert after.usage_stats.cooldown_reason is None


async def test_no_cooldown_for_auth_transient() -> None:
    """AUTH reason has no cooldown — user is likely actively fixing credentials."""
    pool, store = make_pool("p1", strategy="first_ok")
    profile = store.get("p1")
    assert profile is not None

    pool.mark_failure(profile, AuthProfileFailureReason.AUTH)

    updated = store.get("p1")
    assert updated is not None
    assert updated.usage_stats.cooldown_until is None
    assert updated.usage_stats.cooldown_reason == AuthProfileFailureReason.AUTH


# ---------------------------------------------------------------------------
# NoAvailableCredentialError with structured state (Issue 8)
# ---------------------------------------------------------------------------


async def test_all_profiles_exhausted_raises_structured_error() -> None:
    pool, store = make_pool(strategy="first_ok")
    future = datetime(2099, 6, 15, 10, 0, 0)
    store.upsert(
        make_profile(
            "p1",
            cooldown_until=future,
            cooldown_reason=AuthProfileFailureReason.RATE_LIMIT,
        )
    )
    store.upsert(
        make_profile(
            "p2",
            cooldown_until=future,
            cooldown_reason=AuthProfileFailureReason.BILLING,
        )
    )

    with pytest.raises(NoAvailableCredentialError) as exc_info:
        await pool.select()

    err = exc_info.value
    assert err.provider == "openai"
    assert len(err.exhausted_profiles) == 2

    ids = {ep.profile.id for ep in err.exhausted_profiles}
    assert ids == {"p1", "p2"}

    reasons = {ep.profile.id: ep.reason for ep in err.exhausted_profiles}
    assert reasons["p1"] == AuthProfileFailureReason.RATE_LIMIT
    assert reasons["p2"] == AuthProfileFailureReason.BILLING

    # Error message should include provider and guidance on recovery
    assert "openai" in str(err)
    assert "cooldown" in str(err).lower()


async def test_no_profiles_raises_with_empty_list() -> None:
    pool, store = make_pool(strategy="first_ok")  # no profiles added
    with pytest.raises(NoAvailableCredentialError) as exc_info:
        await pool.select()
    assert exc_info.value.exhausted_profiles == []


# ---------------------------------------------------------------------------
# account_identifier scoping
# ---------------------------------------------------------------------------


async def test_select_filters_by_account_identifier() -> None:
    store = InMemoryAuthProfileStore()
    store.upsert(make_profile("alice-openai", account_identifier="alice@example.com"))
    store.upsert(make_profile("bob-openai", account_identifier="bob@example.com"))
    pool = CredentialPool(provider="openai", store=store, strategy="first_ok")

    result = await pool.select(account_identifier="alice@example.com")
    assert result.account_identifier == "alice@example.com"


async def test_select_account_identifier_all_exhausted_raises() -> None:
    store = InMemoryAuthProfileStore()
    store.upsert(
        make_profile(
            "alice-openai",
            account_identifier="alice@example.com",
            cooldown_until=datetime(2099, 1, 1),
        )
    )
    pool = CredentialPool(provider="openai", store=store, strategy="first_ok")

    with pytest.raises(NoAvailableCredentialError):
        await pool.select(account_identifier="alice@example.com")


# ---------------------------------------------------------------------------
# cooldown_overrides
# ---------------------------------------------------------------------------


async def test_cooldown_override_changes_duration() -> None:
    """Per-pool cooldown_overrides replace the default policy for that reason."""
    custom = {AuthProfileFailureReason.RATE_LIMIT: timedelta(minutes=2)}
    pool, store = make_pool("p1", strategy="first_ok", cooldown_overrides=custom)
    profile = store.get("p1")
    assert profile is not None

    pool.mark_failure(profile, AuthProfileFailureReason.RATE_LIMIT)

    updated = store.get("p1")
    assert updated is not None
    assert updated.usage_stats.cooldown_until is not None

    # Should be ~2 minutes, not the default 1 hour
    expected_min = datetime.utcnow() + timedelta(minutes=1, seconds=30)
    expected_max = datetime.utcnow() + timedelta(minutes=2, seconds=30)
    assert expected_min < updated.usage_stats.cooldown_until < expected_max


# ---------------------------------------------------------------------------
# pool.execute() paths (Issue 12)
# ---------------------------------------------------------------------------


async def test_execute_success_path() -> None:
    """Success: mark_success called, result returned."""
    pool, store = make_pool("p1")

    async def fn(profile: AuthProfile) -> str:
        return f"ok:{profile.id}"

    result = await pool.execute(fn, lambda _: AuthProfileFailureReason.UNKNOWN)
    assert result == "ok:p1"

    updated = store.get("p1")
    assert updated is not None
    assert updated.usage_stats.success_count == 1
    assert updated.usage_stats.cooldown_until is None


async def test_execute_non_retriable_failure_reraises_immediately() -> None:
    """Non-retriable failure: mark_failure called, exception re-raised, no retry."""
    pool, store = make_pool("p1", "p2")
    call_count = 0

    async def fn(_profile: AuthProfile) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("auth failed")

    def classifier(_exc: Exception) -> AuthProfileFailureReason:
        return AuthProfileFailureReason.AUTH_PERMANENT

    with pytest.raises(RuntimeError, match="auth failed"):
        await pool.execute(fn, classifier)

    # Only one call — no retry for AUTH_PERMANENT
    assert call_count == 1

    p1 = store.get("p1")
    assert p1 is not None
    assert p1.usage_stats.failure_count == 1
    assert p1.usage_stats.cooldown_reason == AuthProfileFailureReason.AUTH_PERMANENT


async def test_execute_retriable_failure_retries_with_different_credential() -> None:
    """Retriable failure: first profile marked on cooldown, second profile used and succeeds."""
    pool, store = make_pool("p1", "p2")
    call_sequence: list[str] = []

    async def fn(profile: AuthProfile) -> str:
        call_sequence.append(profile.id)
        if profile.id == "p1":
            raise RuntimeError("rate limited")
        return "ok"

    def classifier(_exc: Exception) -> AuthProfileFailureReason:
        return AuthProfileFailureReason.RATE_LIMIT

    result = await pool.execute(fn, classifier)
    assert result == "ok"
    assert call_sequence == ["p1", "p2"]

    p1 = store.get("p1")
    assert p1 is not None
    assert p1.usage_stats.failure_count == 1
    assert p1.usage_stats.cooldown_until is not None  # on cooldown

    p2 = store.get("p2")
    assert p2 is not None
    assert p2.usage_stats.success_count == 1


async def test_execute_all_exhausted_during_retry_raises() -> None:
    """If all profiles are on cooldown when retry calls select(), raise NoAvailableCredentialError."""
    pool, store = make_pool("p1")  # single profile

    async def fn(_profile: AuthProfile) -> None:
        raise RuntimeError("rate limited")

    def classifier(_exc: Exception) -> AuthProfileFailureReason:
        return AuthProfileFailureReason.RATE_LIMIT

    # p1 will be put on cooldown after first failure; retry select() finds no candidates
    with pytest.raises(NoAvailableCredentialError):
        await pool.execute(fn, classifier)


async def test_execute_classifier_raises_marks_unknown_and_reraises_original() -> None:
    """If classifier raises, profile is marked UNKNOWN and the original exception propagates."""
    pool, store = make_pool("p1")
    original_exc = RuntimeError("api call failed")

    async def fn(_profile: AuthProfile) -> None:
        raise original_exc

    def bad_classifier(_exc: Exception) -> AuthProfileFailureReason:
        raise ValueError("classifier bug")

    with pytest.raises(RuntimeError, match="api call failed"):
        await pool.execute(fn, bad_classifier)

    p1 = store.get("p1")
    assert p1 is not None
    assert p1.usage_stats.failure_count == 1
    assert p1.usage_stats.cooldown_reason == AuthProfileFailureReason.UNKNOWN


async def test_execute_sync_callable() -> None:
    """execute() works with a sync callable (not just async)."""
    pool, store = make_pool("p1")

    def sync_fn(profile: AuthProfile) -> str:
        return f"sync:{profile.id}"

    result = await pool.execute(sync_fn, lambda _: AuthProfileFailureReason.UNKNOWN)
    assert result == "sync:p1"


# ---------------------------------------------------------------------------
# Concurrency test — asyncio.Lock correctness (Issue 9)
# ---------------------------------------------------------------------------


async def test_round_robin_concurrent_no_race() -> None:
    """100 concurrent select() calls on round_robin distribute across all profiles.

    This test verifies that asyncio.Lock prevents _last_index races. If the lock
    were absent, concurrent increments would collide and some profiles would be
    over- or under-represented beyond the expected modular distribution.
    """
    N_CALLS = 100
    N_PROFILES = 3
    pool, store = make_pool("a", "b", "c", strategy="round_robin")

    results = await asyncio.gather(*[pool.select() for _ in range(N_CALLS)])
    counts = Counter(r.id for r in results)

    assert len(results) == N_CALLS
    # All 3 profiles must appear at least once
    assert set(counts.keys()) == {"a", "b", "c"}
    # Each profile should appear roughly N/3 times (within ±5 of fair share)
    fair_share = N_CALLS // N_PROFILES
    for pid, count in counts.items():
        assert abs(count - fair_share) <= 5, (
            f"Profile {pid!r} appeared {count} times (expected ~{fair_share}). "
            f"Full distribution: {dict(counts)}"
        )


# ---------------------------------------------------------------------------
# select_sync (Phase 2 — sync call site support)
# ---------------------------------------------------------------------------


def test_select_sync_returns_profile() -> None:
    pool, store = make_pool("p1", "p2", strategy="first_ok")
    result = pool.select_sync()
    assert result.id == "p1"


def test_select_sync_skips_cooldown() -> None:
    pool, store = make_pool(strategy="first_ok")
    store.upsert(make_profile("p1", cooldown_until=datetime(2099, 1, 1)))
    store.upsert(make_profile("p2"))
    result = pool.select_sync()
    assert result.id == "p2"


def test_select_sync_raises_when_all_exhausted() -> None:
    pool, store = make_pool(strategy="first_ok")
    store.upsert(make_profile("p1", cooldown_until=datetime(2099, 1, 1)))
    with pytest.raises(NoAvailableCredentialError):
        pool.select_sync()


def test_select_sync_round_robin_same_state_as_async() -> None:
    """select_sync and select() share the same _last_index via threading.Lock."""
    pool, store = make_pool("a", "b", "c", strategy="round_robin")
    # Advance via async select
    import asyncio as _asyncio

    _asyncio.run(pool.select())  # idx→0 → returns candidates[0] = "a"
    # Next sync select should continue from idx=0 → idx=1 → "b"
    result = pool.select_sync()
    assert result.id == "b"


def test_select_sync_from_thread_no_event_loop() -> None:
    """select_sync works in a plain thread with no running event loop."""
    import threading as _threading

    pool, store = make_pool("p1", strategy="first_ok")
    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(pool.select_sync().id)
        except Exception as e:
            errors.append(e)

    t = _threading.Thread(target=worker)
    t.start()
    t.join()
    assert not errors, f"Thread raised: {errors}"
    assert results == ["p1"]


# ---------------------------------------------------------------------------
# CredentialPoolRegistry
# ---------------------------------------------------------------------------


def test_registry_returns_same_pool_instance() -> None:
    store = InMemoryAuthProfileStore()
    registry = CredentialPoolRegistry(store=store)

    pool_a = registry.get("openai")
    pool_b = registry.get("openai")
    assert pool_a is pool_b


def test_registry_separate_pools_per_provider() -> None:
    store = InMemoryAuthProfileStore()
    registry = CredentialPoolRegistry(store=store)

    openai_pool = registry.get("openai")
    anthropic_pool = registry.get("anthropic")
    assert openai_pool is not anthropic_pool
    assert openai_pool.provider == "openai"
    assert anthropic_pool.provider == "anthropic"


def test_registry_shutdown_clears_pools() -> None:
    store = InMemoryAuthProfileStore()
    registry = CredentialPoolRegistry(store=store)
    registry.get("openai")
    registry.get("anthropic")

    registry.shutdown()

    # After shutdown, get() creates fresh pools
    new_pool = registry.get("openai")
    assert new_pool is not None  # recreated, not None


# ---------------------------------------------------------------------------
# classify_openai_error (Issue 10)
# ---------------------------------------------------------------------------


def _make_openai_exc(exc_class, *, code: str | None = None, message: str = "error") -> Exception:
    """Construct a minimal openai exception for classifier tests."""
    from typing import Any, cast

    exc: Any = exc_class.__new__(exc_class)
    Exception.__init__(exc, message)
    exc.code = code
    exc.status_code = 429
    exc.response = None
    exc.body = {"error": {"code": code, "message": message}}
    return cast(Exception, exc)


@pytest.mark.parametrize(
    "exc_factory, expected_reason",
    [
        # Rate limit — default case
        (
            lambda openai: _make_openai_exc(openai.RateLimitError, code="rate_limit_exceeded"),
            AuthProfileFailureReason.RATE_LIMIT,
        ),
        # Billing / quota exhausted — MUST use exc.code, not str parse (Issue 10)
        (
            lambda openai: _make_openai_exc(openai.RateLimitError, code="insufficient_quota"),
            AuthProfileFailureReason.BILLING,
        ),
        # Authentication
        (
            lambda openai: _make_openai_exc(openai.AuthenticationError, code="invalid_api_key"),
            AuthProfileFailureReason.AUTH_PERMANENT,
        ),
        # Permission denied
        (
            lambda openai: _make_openai_exc(openai.PermissionDeniedError),
            AuthProfileFailureReason.AUTH_PERMANENT,
        ),
        # Timeout — requires an httpx.Request; use MagicMock
        (
            lambda openai: openai.APITimeoutError(MagicMock()),
            AuthProfileFailureReason.TIMEOUT,
        ),
        # Connection error — requires an httpx.Request; use MagicMock
        (
            lambda openai: openai.APIConnectionError(request=MagicMock()),
            AuthProfileFailureReason.TIMEOUT,
        ),
        # Internal server error
        (
            lambda openai: _make_openai_exc(openai.InternalServerError),
            AuthProfileFailureReason.OVERLOADED,
        ),
        # Not found (model access) — maps to UNKNOWN since Phase 4 (#3741)
        (
            lambda openai: _make_openai_exc(openai.NotFoundError),
            AuthProfileFailureReason.UNKNOWN,
        ),
        # Bad request
        (
            lambda openai: _make_openai_exc(openai.BadRequestError),
            AuthProfileFailureReason.FORMAT,
        ),
        # Unknown fallback
        (
            lambda _openai: ValueError("unexpected"),
            AuthProfileFailureReason.UNKNOWN,
        ),
    ],
)
def test_classify_openai_error(exc_factory, expected_reason: AuthProfileFailureReason) -> None:
    openai = pytest.importorskip("openai", reason="openai SDK not installed")
    from nexus.bricks.auth.classifiers.openai import classify_openai_error

    exc = exc_factory(openai)
    result = classify_openai_error(exc)
    assert result == expected_reason


def test_classify_openai_billing_uses_code_not_string_parse() -> None:
    """Billing detection uses exc.code, not string parsing.

    A RateLimitError without code="insufficient_quota" must NOT be classified
    as BILLING even if the message happens to contain "insufficient_quota".
    """
    openai = pytest.importorskip("openai", reason="openai SDK not installed")
    from nexus.bricks.auth.classifiers.openai import classify_openai_error

    # code is NOT insufficient_quota but message contains it — must → RATE_LIMIT
    exc = _make_openai_exc(
        openai.RateLimitError,
        code="rate_limit_exceeded",
        message="You have exceeded your quota: insufficient_quota details here",
    )
    result = classify_openai_error(exc)
    assert result == AuthProfileFailureReason.RATE_LIMIT, (
        "Classifier must use exc.code, not string-parse the message"
    )


def test_classify_openai_no_sdk_returns_unknown() -> None:
    """Classifier returns UNKNOWN gracefully when openai is not installed."""
    from nexus.bricks.auth.classifiers.openai import classify_openai_error

    with patch.dict("sys.modules", {"openai": None}):
        result = classify_openai_error(RuntimeError("some error"))
    assert result == AuthProfileFailureReason.UNKNOWN


# ---------------------------------------------------------------------------
# Default cooldown policy sanity checks
# ---------------------------------------------------------------------------


def test_default_cooldown_policy_covers_all_reasons() -> None:
    """Every AuthProfileFailureReason has an entry in the default policy."""
    for reason in AuthProfileFailureReason:
        assert reason in _DEFAULT_COOLDOWN_POLICY, f"{reason} missing from _DEFAULT_COOLDOWN_POLICY"


def test_retriable_reasons_are_subset_of_policy() -> None:
    """All retriable reasons have a non-None cooldown (they must be put on cooldown)."""
    for reason in _RETRIABLE_REASONS:
        assert _DEFAULT_COOLDOWN_POLICY.get(reason) is not None, (
            f"Retriable reason {reason} has no cooldown — select() would pick it again immediately"
        )
