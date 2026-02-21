"""Tests for Astraea request classifier (Issue #1274).

Tests tier-to-class mapping, cost demotion, IO promotion, and starvation.
"""

from nexus.services.scheduler.constants import PriorityClass, PriorityTier, RequestState
from nexus.services.scheduler.policies.classifier import (
    classify_request,
    should_promote_for_starvation,
)


class TestTierToClassMapping:
    """Test base tier → PriorityClass mapping."""

    def test_critical_maps_to_interactive(self):
        assert classify_request(PriorityTier.CRITICAL) == PriorityClass.INTERACTIVE

    def test_high_maps_to_interactive(self):
        assert classify_request(PriorityTier.HIGH) == PriorityClass.INTERACTIVE

    def test_normal_maps_to_batch(self):
        assert classify_request(PriorityTier.NORMAL) == PriorityClass.BATCH

    def test_low_maps_to_background(self):
        assert classify_request(PriorityTier.LOW) == PriorityClass.BACKGROUND

    def test_best_effort_maps_to_background(self):
        assert classify_request(PriorityTier.BEST_EFFORT) == PriorityClass.BACKGROUND


class TestCostDemotion:
    """Test INTERACTIVE → BATCH demotion when cost exceeds threshold."""

    def test_no_demotion_under_threshold(self):
        result = classify_request(
            PriorityTier.HIGH,
            accumulated_cost=50.0,
            cost_threshold=100.0,
        )
        assert result == PriorityClass.INTERACTIVE

    def test_demotion_over_threshold(self):
        result = classify_request(
            PriorityTier.HIGH,
            accumulated_cost=150.0,
            cost_threshold=100.0,
        )
        assert result == PriorityClass.BATCH

    def test_demotion_only_applies_to_interactive(self):
        """BATCH and BACKGROUND should not be affected by cost."""
        result = classify_request(
            PriorityTier.NORMAL,
            accumulated_cost=150.0,
            cost_threshold=100.0,
        )
        assert result == PriorityClass.BATCH  # Already BATCH, no change


class TestIOPromotion:
    """Test BACKGROUND → BATCH promotion for IO_WAIT."""

    def test_io_wait_promotes_background_to_batch(self):
        result = classify_request(
            PriorityTier.LOW,
            request_state=RequestState.IO_WAIT,
        )
        assert result == PriorityClass.BATCH

    def test_non_io_wait_stays_background(self):
        result = classify_request(
            PriorityTier.LOW,
            request_state=RequestState.COMPUTE,
        )
        assert result == PriorityClass.BACKGROUND

    def test_io_wait_string_input(self):
        """Accept string 'io_wait' as well as enum."""
        result = classify_request(PriorityTier.LOW, request_state="io_wait")
        assert result == PriorityClass.BATCH


class TestStarvationPromotion:
    """Test starvation-based promotion."""

    def test_background_promoted_after_threshold(self):
        result = should_promote_for_starvation(1000.0, PriorityClass.BACKGROUND, threshold=900.0)
        assert result == PriorityClass.BATCH

    def test_background_not_promoted_before_threshold(self):
        result = should_promote_for_starvation(500.0, PriorityClass.BACKGROUND, threshold=900.0)
        assert result == PriorityClass.BACKGROUND

    def test_batch_not_promoted(self):
        result = should_promote_for_starvation(2000.0, PriorityClass.BATCH, threshold=900.0)
        assert result == PriorityClass.BATCH

    def test_interactive_not_promoted(self):
        result = should_promote_for_starvation(2000.0, PriorityClass.INTERACTIVE, threshold=900.0)
        assert result == PriorityClass.INTERACTIVE
