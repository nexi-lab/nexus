"""Tests for priority computation logic.

TDD tests for the 4-layer priority system:
1. Base tier ordering
2. Price boost (capped at +2 tiers)
3. Aging (tasks gain priority over time)
4. Max-wait escalation
5. Combined interactions
6. Validation

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nexus.scheduler.constants import (
    AGING_THRESHOLD_SECONDS,
    BOOST_COST_PER_TIER,
    MAX_BOOST_TIERS,
    MAX_WAIT_SECONDS,
    PriorityTier,
)
from nexus.scheduler.models import TaskSubmission
from nexus.scheduler.priority import (
    compute_boost_tiers,
    compute_effective_tier,
    validate_submission,
)

# =============================================================================
# 1. Base Tier Ordering
# =============================================================================


class TestBaseTierOrdering:
    """Test that tiers are correctly ordered."""

    def test_tier_ordering(self):
        """CRITICAL < HIGH < NORMAL < LOW < BEST_EFFORT."""
        assert PriorityTier.CRITICAL < PriorityTier.HIGH
        assert PriorityTier.HIGH < PriorityTier.NORMAL
        assert PriorityTier.NORMAL < PriorityTier.LOW
        assert PriorityTier.LOW < PriorityTier.BEST_EFFORT

    def test_default_tier_is_normal(self):
        """Default effective tier matches NORMAL priority."""
        now = datetime.now(UTC)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
        )
        result = compute_effective_tier(task, enqueued_at=now, now=now)
        assert result == PriorityTier.NORMAL

    def test_critical_tier(self):
        """CRITICAL tier should produce effective_tier = 0."""
        now = datetime.now(UTC)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.CRITICAL,
        )
        result = compute_effective_tier(task, enqueued_at=now, now=now)
        assert result == 0

    def test_best_effort_tier(self):
        """BEST_EFFORT tier should produce effective_tier = 4."""
        now = datetime.now(UTC)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.BEST_EFFORT,
        )
        result = compute_effective_tier(task, enqueued_at=now, now=now)
        assert result == 4


# =============================================================================
# 2. Price Boost
# =============================================================================


class TestPriceBoost:
    """Test capped price boost (max +2 tiers)."""

    def test_no_boost(self):
        """Zero boost should not change effective tier."""
        assert compute_boost_tiers(Decimal("0")) == 0

    def test_one_tier_boost(self):
        """Paying for one tier boost."""
        assert compute_boost_tiers(BOOST_COST_PER_TIER) == 1

    def test_two_tier_boost(self):
        """Paying for maximum two tier boost."""
        assert compute_boost_tiers(BOOST_COST_PER_TIER * 2) == 2

    def test_boost_capped_at_max(self):
        """Paying more than max should still cap at MAX_BOOST_TIERS."""
        assert compute_boost_tiers(BOOST_COST_PER_TIER * 10) == MAX_BOOST_TIERS

    def test_partial_tier_boost_rounds_down(self):
        """Partial payment should round down (floor)."""
        partial = BOOST_COST_PER_TIER * Decimal("1.5")
        assert compute_boost_tiers(partial) == 1

    def test_boost_applied_to_effective_tier(self):
        """Boost should lower effective tier (higher priority)."""
        now = datetime.now(UTC)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.LOW,  # tier 3
            boost_amount=BOOST_COST_PER_TIER * 2,  # +2 boost
        )
        result = compute_effective_tier(task, enqueued_at=now, now=now)
        assert result == 1  # 3 - 2 = 1 (HIGH)

    def test_boost_cannot_go_below_critical(self):
        """Even with max boost, effective tier cannot go below 0 (CRITICAL)."""
        now = datetime.now(UTC)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.HIGH,  # tier 1
            boost_amount=BOOST_COST_PER_TIER * 2,  # +2 boost -> 1-2 = -1
        )
        result = compute_effective_tier(task, enqueued_at=now, now=now)
        assert result == 0  # Clamped to CRITICAL


# =============================================================================
# 3. Aging
# =============================================================================


class TestAging:
    """Test anti-starvation aging mechanism."""

    def test_no_aging_when_fresh(self):
        """Tasks that haven't waited don't get aging boost."""
        now = datetime.now(UTC)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.LOW,
        )
        result = compute_effective_tier(task, enqueued_at=now, now=now)
        assert result == PriorityTier.LOW  # No aging

    def test_aging_after_threshold(self):
        """Task waiting past threshold gets +1 tier boost."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=AGING_THRESHOLD_SECONDS + 1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.LOW,  # tier 3
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        assert result == 2  # 3 - 1 aging = 2 (NORMAL)

    def test_aging_multiple_thresholds(self):
        """Task waiting 2x threshold gets +2 tier boost."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=AGING_THRESHOLD_SECONDS * 2 + 1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.BEST_EFFORT,  # tier 4
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        assert result == 2  # 4 - 2 aging = 2 (NORMAL)

    def test_aging_cannot_go_below_critical(self):
        """Even with long wait, effective tier cannot go below 0."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=AGING_THRESHOLD_SECONDS * 100)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.NORMAL,  # tier 2
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        assert result == 0  # Clamped to CRITICAL


# =============================================================================
# 4. Max-Wait Escalation
# =============================================================================


class TestMaxWaitEscalation:
    """Test that tasks exceeding MAX_WAIT_SECONDS get escalated."""

    def test_max_wait_escalation(self):
        """Task waiting past MAX_WAIT_SECONDS should escalate to HIGH."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=MAX_WAIT_SECONDS + 1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.BEST_EFFORT,  # tier 4
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        # Should be at most HIGH (1), could be lower from aging
        assert result <= PriorityTier.HIGH

    def test_max_wait_does_not_affect_already_high(self):
        """Already HIGH or CRITICAL tasks are not changed by max-wait."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=MAX_WAIT_SECONDS + 1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.CRITICAL,
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        assert result == 0  # Still CRITICAL


# =============================================================================
# 5. Combined Interactions
# =============================================================================


class TestCombinedPriority:
    """Test interactions between boost, aging, and max-wait."""

    def test_boost_plus_aging(self):
        """Boost and aging should both reduce effective tier."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=AGING_THRESHOLD_SECONDS + 1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.BEST_EFFORT,  # tier 4
            boost_amount=BOOST_COST_PER_TIER,  # +1 boost
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        assert result == 2  # 4 - 1 boost - 1 aging = 2

    def test_all_layers_combined(self):
        """All priority layers applied together."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=AGING_THRESHOLD_SECONDS * 2 + 1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.BEST_EFFORT,  # tier 4
            boost_amount=BOOST_COST_PER_TIER * 2,  # +2 boost
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        # 4 - 2 boost - 2 aging = 0 (CRITICAL)
        assert result == 0

    def test_effective_tier_always_non_negative(self):
        """effective_tier should never be negative, regardless of inputs."""
        now = datetime.now(UTC)
        enqueued = now - timedelta(seconds=MAX_WAIT_SECONDS * 10)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            priority=PriorityTier.NORMAL,
            boost_amount=BOOST_COST_PER_TIER * MAX_BOOST_TIERS,
        )
        result = compute_effective_tier(task, enqueued_at=enqueued, now=now)
        assert result >= 0


# =============================================================================
# 6. Validation
# =============================================================================


class TestValidation:
    """Test submission validation."""

    def test_valid_submission(self):
        """Valid submission should not raise."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
        )
        validate_submission(task)  # Should not raise

    def test_deadline_in_past_raises(self):
        """Deadline in the past should raise ValueError."""
        past = datetime.now(UTC) - timedelta(hours=1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            deadline=past,
        )
        with pytest.raises(ValueError, match="deadline"):
            validate_submission(task)

    def test_negative_boost_raises(self):
        """Negative boost amount should raise ValueError."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            boost_amount=Decimal("-0.01"),
        )
        with pytest.raises(ValueError, match="boost"):
            validate_submission(task)

    def test_boost_exceeding_max_raises(self):
        """Boost exceeding max allowed should raise ValueError."""
        max_boost_cost = BOOST_COST_PER_TIER * MAX_BOOST_TIERS
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            boost_amount=max_boost_cost + Decimal("0.01"),
        )
        with pytest.raises(ValueError, match="boost"):
            validate_submission(task)

    def test_boost_at_max_is_valid(self):
        """Boost at exactly max should be valid."""
        max_boost_cost = BOOST_COST_PER_TIER * MAX_BOOST_TIERS
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            boost_amount=max_boost_cost,
        )
        validate_submission(task)  # Should not raise

    def test_empty_agent_id_raises(self):
        """Empty agent_id should raise ValueError."""
        task = TaskSubmission(
            agent_id="",
            executor_id="agent-b",
            task_type="test",
        )
        with pytest.raises(ValueError, match="agent_id"):
            validate_submission(task)

    def test_empty_executor_id_raises(self):
        """Empty executor_id should raise ValueError."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="",
            task_type="test",
        )
        with pytest.raises(ValueError, match="executor_id"):
            validate_submission(task)

    def test_empty_task_type_raises(self):
        """Empty task_type should raise ValueError."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="",
        )
        with pytest.raises(ValueError, match="task_type"):
            validate_submission(task)
