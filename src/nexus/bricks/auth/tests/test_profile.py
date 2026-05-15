"""Tests for AuthProfile, ProfileUsageStats, AuthProfileFailureReason, InMemoryAuthProfileStore.

Coverage: exhaustive but trivial per acceptance criteria.
"""

from __future__ import annotations

from nexus.bricks.auth.profile import (
    RAW_ERROR_MAX_LEN,
    AuthProfile,
    AuthProfileFailureReason,
    AuthProfileStore,
    InMemoryAuthProfileStore,
    ProfileUsageStats,
)

# ---------------------------------------------------------------------------
# AuthProfileFailureReason enum
# ---------------------------------------------------------------------------


class TestFailureReasonEnum:
    def test_all_expected_values_present(self) -> None:
        expected = {
            "auth",
            "auth_permanent",
            "format",
            "overloaded",
            "rate_limit",
            "billing",
            "timeout",
            "session_expired",
            "mfa_required",
            "proxy_or_tls",
            "upstream_cli_missing",
            "scope_insufficient",
            "clock_skew",
            "unknown",
        }
        actual = {e.value for e in AuthProfileFailureReason}
        assert expected == actual

    def test_new_values_accessible(self) -> None:
        assert AuthProfileFailureReason.MFA_REQUIRED.value == "mfa_required"
        assert AuthProfileFailureReason.PROXY_OR_TLS.value == "proxy_or_tls"
        assert AuthProfileFailureReason.UPSTREAM_CLI_MISSING.value == "upstream_cli_missing"
        assert AuthProfileFailureReason.SCOPE_INSUFFICIENT.value == "scope_insufficient"
        assert AuthProfileFailureReason.CLOCK_SKEW.value == "clock_skew"


def test_model_not_found_enum_removed():
    """MODEL_NOT_FOUND was deprecated and removed in Phase 4 (#3741)."""
    assert not hasattr(AuthProfileFailureReason, "MODEL_NOT_FOUND")
    names = {member.name for member in AuthProfileFailureReason}
    assert "MODEL_NOT_FOUND" not in names


# ---------------------------------------------------------------------------
# ProfileUsageStats
# ---------------------------------------------------------------------------


class TestProfileUsageStats:
    def test_defaults(self) -> None:
        stats = ProfileUsageStats()
        assert stats.last_used_at is None
        assert stats.success_count == 0
        assert stats.failure_count == 0
        assert stats.cooldown_until is None
        assert stats.cooldown_reason is None
        assert stats.disabled_until is None
        assert stats.raw_error is None

    def test_raw_error_stored(self) -> None:
        stats = ProfileUsageStats(raw_error="something broke")
        assert stats.raw_error == "something broke"


# ---------------------------------------------------------------------------
# AuthProfile
# ---------------------------------------------------------------------------


class TestAuthProfile:
    def test_construction(self) -> None:
        p = AuthProfile(
            id="google/alice@example.com",
            provider="google",
            account_identifier="alice@example.com",
            backend="nexus-token-manager",
            backend_key="google/alice@example.com",
        )
        assert p.id == "google/alice@example.com"
        assert p.provider == "google"
        assert p.backend == "nexus-token-manager"
        assert p.sync_ttl_seconds == 300  # default

    def test_usage_stats_default_factory(self) -> None:
        p1 = AuthProfile(
            id="a",
            provider="p",
            account_identifier="a",
            backend="b",
            backend_key="k",
        )
        p2 = AuthProfile(
            id="b",
            provider="p",
            account_identifier="b",
            backend="b",
            backend_key="k",
        )
        # Each should have its own stats instance
        p1.usage_stats.success_count = 5
        assert p2.usage_stats.success_count == 0


# ---------------------------------------------------------------------------
# InMemoryAuthProfileStore
# ---------------------------------------------------------------------------


class TestInMemoryStore:
    def test_protocol_conformance(self) -> None:
        store = InMemoryAuthProfileStore()
        assert isinstance(store, AuthProfileStore)

    def test_crud(self) -> None:
        store = InMemoryAuthProfileStore()
        p = AuthProfile(
            id="test",
            provider="openai",
            account_identifier="test",
            backend="b",
            backend_key="k",
        )
        store.upsert(p)
        assert store.get("test") is p
        assert len(store.list()) == 1
        store.delete("test")
        assert store.get("test") is None

    def test_list_by_provider(self) -> None:
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="a",
                provider="openai",
                account_identifier="a",
                backend="b",
                backend_key="k",
            )
        )
        store.upsert(
            AuthProfile(
                id="b",
                provider="google",
                account_identifier="b",
                backend="b",
                backend_key="k",
            )
        )
        assert len(store.list(provider="openai")) == 1

    def test_mark_success(self) -> None:
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="a",
                provider="p",
                account_identifier="a",
                backend="b",
                backend_key="k",
            )
        )
        store.mark_success("a")
        p = store.get("a")
        assert p is not None
        assert p.usage_stats.success_count == 1
        assert p.usage_stats.last_used_at is not None

    def test_mark_failure(self) -> None:
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="a",
                provider="p",
                account_identifier="a",
                backend="b",
                backend_key="k",
            )
        )
        store.mark_failure("a", AuthProfileFailureReason.BILLING, raw_error="quota exceeded")
        p = store.get("a")
        assert p is not None
        assert p.usage_stats.failure_count == 1
        assert p.usage_stats.cooldown_reason == AuthProfileFailureReason.BILLING
        assert p.usage_stats.raw_error == "quota exceeded"

    def test_mark_failure_truncates_raw_error(self) -> None:
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="a",
                provider="p",
                account_identifier="a",
                backend="b",
                backend_key="k",
            )
        )
        store.mark_failure("a", AuthProfileFailureReason.UNKNOWN, raw_error="x" * 1000)
        p = store.get("a")
        assert p is not None
        assert p.usage_stats.raw_error is not None
        assert len(p.usage_stats.raw_error) == RAW_ERROR_MAX_LEN

    def test_mark_on_nonexistent(self) -> None:
        store = InMemoryAuthProfileStore()
        store.mark_success("nope")  # should not raise
        store.mark_failure("nope", AuthProfileFailureReason.UNKNOWN)
