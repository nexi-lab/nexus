"""Tests for TokenBucketLimiter (Issue #2749).

Comprehensive tests covering: under/at/over rate, refill timing,
burst capacity, empty bucket rejection, and clock mockability.
"""

import pytest

from nexus.services.scheduler.policies.rate_limiter import TokenBucketLimiter


class TestTokenBucketBasic:
    """Test basic token bucket behavior."""

    def test_under_rate_passes(self):
        """Submissions under the rate limit are allowed."""
        limiter = TokenBucketLimiter(rate=10.0)
        assert limiter.try_acquire("agent-a") is True

    def test_at_rate_passes(self):
        """Exactly 'burst' submissions in a burst are allowed."""
        limiter = TokenBucketLimiter(rate=5.0, burst=5.0)
        for _ in range(5):
            assert limiter.try_acquire("agent-a") is True

    def test_over_rate_rejects(self):
        """Submissions beyond burst capacity are rejected."""
        limiter = TokenBucketLimiter(rate=3.0, burst=3.0)
        for _ in range(3):
            limiter.try_acquire("agent-a")
        assert limiter.try_acquire("agent-a") is False

    def test_different_agents_independent(self):
        """Each agent has its own bucket."""
        limiter = TokenBucketLimiter(rate=1.0, burst=1.0)
        assert limiter.try_acquire("agent-a") is True
        assert limiter.try_acquire("agent-b") is True
        # agent-a is now empty
        assert limiter.try_acquire("agent-a") is False
        # agent-b is also empty
        assert limiter.try_acquire("agent-b") is False


class TestTokenBucketRefill:
    """Test token refill over time."""

    def test_tokens_refill_after_time(self):
        """Tokens are replenished based on elapsed time."""
        time_now = 100.0

        def clock() -> float:
            return time_now

        limiter = TokenBucketLimiter(rate=10.0, burst=10.0, clock=clock)

        # Drain all tokens
        for _ in range(10):
            limiter.try_acquire("agent-a")
        assert limiter.try_acquire("agent-a") is False

        # Advance time by 1 second — should refill 10 tokens
        time_now = 101.0
        assert limiter.try_acquire("agent-a") is True

    def test_partial_refill(self):
        """Partial time refills partial tokens."""
        time_now = 0.0

        def clock() -> float:
            return time_now

        limiter = TokenBucketLimiter(rate=10.0, burst=10.0, clock=clock)

        # Drain all tokens
        for _ in range(10):
            limiter.try_acquire("agent-a")

        # Advance 0.5 seconds — should refill 5 tokens
        time_now = 0.5
        for _ in range(5):
            assert limiter.try_acquire("agent-a") is True
        assert limiter.try_acquire("agent-a") is False

    def test_refill_capped_at_burst(self):
        """Tokens never exceed burst capacity even after long wait."""
        time_now = 0.0

        def clock() -> float:
            return time_now

        limiter = TokenBucketLimiter(rate=5.0, burst=5.0, clock=clock)

        # Use 1 token
        limiter.try_acquire("agent-a")

        # Wait a very long time
        time_now = 1000.0

        # Should only have burst (5) tokens, not 5000+
        count = 0
        while limiter.try_acquire("agent-a"):
            count += 1
        assert count == 5


class TestTokenBucketBurst:
    """Test burst capacity."""

    def test_burst_greater_than_rate(self):
        """Burst capacity can exceed the per-second rate."""
        limiter = TokenBucketLimiter(rate=5.0, burst=20.0)
        count = 0
        while limiter.try_acquire("agent-a"):
            count += 1
        assert count == 20

    def test_burst_less_than_rate(self):
        """Burst capacity can be less than the per-second rate."""
        limiter = TokenBucketLimiter(rate=100.0, burst=3.0)
        count = 0
        while limiter.try_acquire("agent-a"):
            count += 1
        assert count == 3


class TestTokenBucketEmptyBucket:
    """Test empty bucket rejection."""

    def test_empty_bucket_rejects(self):
        """Empty bucket consistently rejects without time advancement."""
        time_now = 0.0

        def clock() -> float:
            return time_now

        limiter = TokenBucketLimiter(rate=1.0, burst=1.0, clock=clock)
        limiter.try_acquire("agent-a")

        # Multiple attempts at same time should all fail
        for _ in range(5):
            assert limiter.try_acquire("agent-a") is False


class TestTokenBucketConfiguration:
    """Test configuration validation."""

    def test_invalid_rate_raises(self):
        with pytest.raises(ValueError, match="rate must be positive"):
            TokenBucketLimiter(rate=0)

    def test_negative_rate_raises(self):
        with pytest.raises(ValueError, match="rate must be positive"):
            TokenBucketLimiter(rate=-1.0)

    def test_invalid_burst_raises(self):
        with pytest.raises(ValueError, match="burst must be positive"):
            TokenBucketLimiter(rate=10.0, burst=0)

    def test_properties(self):
        limiter = TokenBucketLimiter(rate=5.0, burst=20.0)
        assert limiter.rate == 5.0
        assert limiter.burst == 20.0

    def test_default_burst_equals_rate(self):
        limiter = TokenBucketLimiter(rate=7.0)
        assert limiter.burst == 7.0


class TestTokenBucketClockMock:
    """Test clock injection for deterministic testing."""

    def test_custom_clock(self):
        """Clock function is called for token operations."""
        calls: list[float] = []
        time_now = 42.0

        def clock() -> float:
            calls.append(time_now)
            return time_now

        limiter = TokenBucketLimiter(rate=10.0, clock=clock)
        limiter.try_acquire("agent-a")
        assert len(calls) > 0
        assert all(c == 42.0 for c in calls)
