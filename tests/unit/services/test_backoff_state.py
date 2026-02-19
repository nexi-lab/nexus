"""Tests for BackoffState and compute_next_backoff (Issue #2059).

TDD: Tests written FIRST for the decorrelated jitter backoff algorithm.
"""

from __future__ import annotations

import pytest

from nexus.services.protocols.brick_reconciler import (
    BackoffState,
    BrickReconcilerConfig,
    compute_next_backoff,
    reset_backoff,
)


class TestBackoffStateImmutability:
    """Verify BackoffState is frozen and slot-based."""

    def test_frozen(self) -> None:
        state = BackoffState()
        with pytest.raises(AttributeError):
            state.attempt = 5  # type: ignore[misc]

    def test_defaults(self) -> None:
        state = BackoffState()
        assert state.attempt == 0
        assert state.last_delay == 0.0
        assert state.next_retry_at == 0.0


class TestComputeNextBackoff:
    """Test the decorrelated jitter backoff algorithm."""

    @pytest.fixture
    def config(self) -> BrickReconcilerConfig:
        return BrickReconcilerConfig(base_delay=1.0, max_delay=600.0)

    def test_first_attempt_uses_base_delay(self, config: BrickReconcilerConfig) -> None:
        """First attempt: delay in [base_delay, base_delay * 3]."""
        state = BackoffState()
        for _ in range(100):  # statistical check
            next_state = compute_next_backoff(state, config, _now=0.0)
            assert next_state.attempt == 1
            assert config.base_delay <= next_state.last_delay <= config.base_delay * 3
            assert next_state.next_retry_at == next_state.last_delay  # _now=0

    def test_attempt_counter_increments(self, config: BrickReconcilerConfig) -> None:
        state = BackoffState()
        for expected in range(1, 6):
            state = compute_next_backoff(state, config, _now=0.0)
            assert state.attempt == expected

    def test_delay_trends_upward(self, config: BrickReconcilerConfig) -> None:
        """Over many runs, average delay should trend upward.

        Decorrelated jitter is intentionally non-monotonic per AWS algorithm.
        We run multiple trials and check the statistical trend.
        """
        upward_count = 0
        for _ in range(10):
            delays: list[float] = []
            state = BackoffState()
            for _ in range(20):
                state = compute_next_backoff(state, config, _now=0.0)
                delays.append(state.last_delay)
            avg_early = sum(delays[:5]) / 5
            avg_late = sum(delays[15:]) / 5
            if avg_late >= avg_early:
                upward_count += 1
        # At least 6/10 trials should trend upward (probabilistic)
        assert upward_count >= 6, f"Only {upward_count}/10 trials trended upward"

    def test_delay_capped_at_max(self) -> None:
        """Delay should never exceed max_delay."""
        config = BrickReconcilerConfig(base_delay=1.0, max_delay=10.0)
        state = BackoffState(attempt=50, last_delay=1000.0)  # Huge last_delay
        for _ in range(100):
            next_state = compute_next_backoff(state, config, _now=0.0)
            assert next_state.last_delay <= config.max_delay
            state = next_state

    def test_jitter_produces_different_values(self, config: BrickReconcilerConfig) -> None:
        """Decorrelated jitter should NOT be deterministic."""
        state = BackoffState(attempt=3, last_delay=5.0)
        results = {compute_next_backoff(state, config, _now=0.0).last_delay for _ in range(50)}
        # With jitter, we should get multiple distinct values
        assert len(results) > 1

    def test_next_retry_at_uses_now(self, config: BrickReconcilerConfig) -> None:
        """next_retry_at should be now + delay."""
        state = BackoffState()
        next_state = compute_next_backoff(state, config, _now=100.0)
        assert next_state.next_retry_at == pytest.approx(100.0 + next_state.last_delay)

    def test_original_state_unchanged(self, config: BrickReconcilerConfig) -> None:
        """compute_next_backoff must not mutate the input state."""
        original = BackoffState(attempt=2, last_delay=3.0, next_retry_at=50.0)
        compute_next_backoff(original, config, _now=0.0)
        assert original.attempt == 2
        assert original.last_delay == 3.0
        assert original.next_retry_at == 50.0


class TestResetBackoff:
    """Test reset_backoff returns a zero-state."""

    def test_returns_zero_state(self) -> None:
        state = reset_backoff()
        assert state.attempt == 0
        assert state.last_delay == 0.0
        assert state.next_retry_at == 0.0

    def test_returns_new_instance(self) -> None:
        a = reset_backoff()
        b = reset_backoff()
        assert a == b  # equal values
        assert a is not b  # distinct objects


class TestBrickReconcilerConfig:
    """Test BrickReconcilerConfig defaults and immutability."""

    def test_defaults(self) -> None:
        config = BrickReconcilerConfig()
        assert config.health_check_interval == 30.0
        assert config.base_delay == 1.0
        assert config.max_delay == 600.0
        assert config.max_attempts == 10
        assert config.health_check_timeout == 5.0

    def test_frozen(self) -> None:
        config = BrickReconcilerConfig()
        with pytest.raises(AttributeError):
            config.max_attempts = 99  # type: ignore[misc]

    def test_custom_values(self) -> None:
        config = BrickReconcilerConfig(
            health_check_interval=10.0,
            base_delay=0.5,
            max_delay=300.0,
            max_attempts=5,
            health_check_timeout=3.0,
        )
        assert config.health_check_interval == 10.0
        assert config.max_attempts == 5
