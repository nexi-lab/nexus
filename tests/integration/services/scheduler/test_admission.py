"""Tests for AdmissionPolicy (Issue #2749).

Tests the composed admission policy that combines rate limiting
and fair-share concurrency checks.
"""

import pytest

from nexus.services.scheduler.exceptions import CapacityExceeded, RateLimitExceeded
from nexus.services.scheduler.policies.admission import AdmissionPolicy
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.policies.rate_limiter import TokenBucketLimiter


@pytest.fixture
def fair_share() -> FairShareCounter:
    return FairShareCounter(default_max_concurrent=5)


@pytest.fixture
def rate_limiter() -> TokenBucketLimiter:
    return TokenBucketLimiter(rate=10.0, burst=10.0)


@pytest.fixture
def policy(fair_share: FairShareCounter, rate_limiter: TokenBucketLimiter) -> AdmissionPolicy:
    return AdmissionPolicy(fair_share=fair_share, rate_limiter=rate_limiter)


class TestAdmissionPolicyCheck:
    """Test the unified check() method."""

    def test_passes_when_both_allow(self, policy: AdmissionPolicy):
        """No exception when both rate limit and fair-share allow."""
        policy.check("agent-a")  # Should not raise

    def test_rate_limit_checked_first(self):
        """Rate limit is checked before fair-share (cheapest first)."""
        # Both would reject, but rate limit should fire first
        fs = FairShareCounter(default_max_concurrent=1)
        fs.record_start("agent-a")  # At capacity

        time_now = 0.0
        rl = TokenBucketLimiter(rate=1.0, burst=1.0, clock=lambda: time_now)
        rl.try_acquire("agent-a")  # Drain tokens

        policy = AdmissionPolicy(fair_share=fs, rate_limiter=rl)

        with pytest.raises(RateLimitExceeded):
            policy.check("agent-a")

    def test_raises_rate_limit_exceeded(self):
        """RateLimitExceeded when token bucket is empty."""
        time_now = 0.0
        rl = TokenBucketLimiter(rate=1.0, burst=1.0, clock=lambda: time_now)
        rl.try_acquire("agent-a")

        policy = AdmissionPolicy(
            fair_share=FairShareCounter(),
            rate_limiter=rl,
        )

        with pytest.raises(RateLimitExceeded, match="agent-a"):
            policy.check("agent-a")

    def test_raises_capacity_exceeded(self, rate_limiter: TokenBucketLimiter):
        """CapacityExceeded when fair-share is at capacity."""
        fs = FairShareCounter(default_max_concurrent=2)
        fs.record_start("agent-a")
        fs.record_start("agent-a")

        policy = AdmissionPolicy(fair_share=fs, rate_limiter=rate_limiter)

        with pytest.raises(CapacityExceeded, match="at capacity"):
            policy.check("agent-a")

    def test_rate_limit_error_includes_rate(self):
        """RateLimitExceeded message includes the configured rate."""
        time_now = 0.0
        rl = TokenBucketLimiter(rate=42.0, burst=1.0, clock=lambda: time_now)
        rl.try_acquire("agent-x")

        policy = AdmissionPolicy(
            fair_share=FairShareCounter(),
            rate_limiter=rl,
        )

        with pytest.raises(RateLimitExceeded, match="42.0/s"):
            policy.check("agent-x")

    def test_capacity_error_includes_counts(self):
        """CapacityExceeded message includes running/max counts."""
        fs = FairShareCounter(default_max_concurrent=3)
        fs.record_start("agent-b")
        fs.record_start("agent-b")
        fs.record_start("agent-b")

        policy = AdmissionPolicy(
            fair_share=fs,
            rate_limiter=TokenBucketLimiter(rate=100.0),
        )

        with pytest.raises(CapacityExceeded, match="3/3"):
            policy.check("agent-b")


class TestAdmissionPolicyProperties:
    """Test property accessors."""

    def test_fair_share_accessible(self, policy: AdmissionPolicy, fair_share: FairShareCounter):
        assert policy.fair_share is fair_share

    def test_rate_limiter_accessible(
        self, policy: AdmissionPolicy, rate_limiter: TokenBucketLimiter
    ):
        assert policy.rate_limiter is rate_limiter
