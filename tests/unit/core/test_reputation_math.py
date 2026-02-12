"""Unit tests for Bayesian Beta reputation math (Issue #1356).

Pure function tests — no ORM, no database. ~15-20 parametrized cases covering:
- Cold start: Beta(1,1) → score=0.5, confidence=0.0
- All positive (10): Beta(11,1) → score≈0.917
- All negative (10): Beta(1,11) → score≈0.083
- Mixed (5+5): Beta(6,6) → score=0.5
- Time decay: half-life behavior
- Composite score: weighted average of 4 dimensions
- Overflow safety: large alpha+beta values
- Edge: zero interactions → prior
"""

from __future__ import annotations

import math

import pytest

from nexus.core.reputation_math import (
    DEFAULT_HALF_LIFE_SECONDS,
    compute_beta_score,
    compute_composite_score,
    compute_confidence,
    compute_decay_weight,
)


# ---------------------------------------------------------------------------
# 1. compute_beta_score tests
# ---------------------------------------------------------------------------


class TestComputeBetaScore:
    """Test Beta distribution expected value computation."""

    @pytest.mark.parametrize(
        ("alpha", "beta", "expected"),
        [
            # Cold start / prior: Beta(1,1) → 0.5
            (1.0, 1.0, 0.5),
            # All positive (10 feedbacks): Beta(11, 1) → 11/12 ≈ 0.917
            (11.0, 1.0, 11.0 / 12.0),
            # All negative (10 feedbacks): Beta(1, 11) → 1/12 ≈ 0.083
            (1.0, 11.0, 1.0 / 12.0),
            # Mixed equally (5+5): Beta(6, 6) → 0.5
            (6.0, 6.0, 0.5),
            # Strong positive (100+, 5-): Beta(101, 6) → ~0.944
            (101.0, 6.0, 101.0 / 107.0),
            # Weak positive (2+, 1-): Beta(3, 2) → 0.6
            (3.0, 2.0, 0.6),
            # Single positive: Beta(2, 1) → 2/3 ≈ 0.667
            (2.0, 1.0, 2.0 / 3.0),
            # Single negative: Beta(1, 2) → 1/3 ≈ 0.333
            (1.0, 2.0, 1.0 / 3.0),
        ],
        ids=[
            "cold_start",
            "all_positive_10",
            "all_negative_10",
            "mixed_equal",
            "strong_positive",
            "weak_positive",
            "single_positive",
            "single_negative",
        ],
    )
    def test_expected_value(self, alpha: float, beta: float, expected: float) -> None:
        result = compute_beta_score(alpha, beta)
        assert result == pytest.approx(expected, abs=1e-6)

    def test_overflow_safety_large_values(self) -> None:
        """Large alpha+beta values should not overflow."""
        result = compute_beta_score(1_000_000.0, 1_000_000.0)
        assert result == pytest.approx(0.5, abs=1e-6)

    def test_overflow_safety_extreme_skew(self) -> None:
        """Extreme skew toward positive."""
        result = compute_beta_score(1_000_000.0, 1.0)
        assert result == pytest.approx(1.0, abs=1e-3)

    def test_zero_total_returns_prior(self) -> None:
        """Edge case: alpha + beta = 0 returns prior 0.5."""
        assert compute_beta_score(0.0, 0.0) == 0.5

    def test_negative_total_returns_prior(self) -> None:
        """Edge case: negative total returns prior 0.5."""
        assert compute_beta_score(-1.0, 0.0) == 0.5


# ---------------------------------------------------------------------------
# 2. compute_confidence tests
# ---------------------------------------------------------------------------


class TestComputeConfidence:
    """Test confidence computation from Beta parameters."""

    def test_zero_observations(self) -> None:
        """Beta(1,1) = 0 observations → confidence = 0.0."""
        assert compute_confidence(1.0, 1.0) == 0.0

    def test_one_observation(self) -> None:
        """Beta(2,1) = 1 observation → confidence ≈ 0.41."""
        conf = compute_confidence(2.0, 1.0)
        expected = 1.0 - 1.0 / (1.0 + math.log(2.0))
        assert conf == pytest.approx(expected, abs=1e-6)
        assert 0.3 < conf < 0.5

    def test_ten_observations(self) -> None:
        """10 observations → confidence ≈ 0.71."""
        conf = compute_confidence(6.0, 6.0)  # 10 observations
        assert 0.6 < conf < 0.8

    def test_hundred_observations(self) -> None:
        """100 observations → confidence ≈ 0.82."""
        conf = compute_confidence(51.0, 51.0)  # 100 observations
        assert 0.75 < conf < 0.90

    def test_thousand_observations(self) -> None:
        """1000 observations → confidence ≈ 0.87."""
        conf = compute_confidence(501.0, 501.0)  # 1000 observations
        assert 0.85 < conf < 0.95

    def test_confidence_monotonically_increases(self) -> None:
        """Confidence increases with more observations."""
        confs = [compute_confidence(1.0 + n, 1.0) for n in range(0, 101, 10)]
        for i in range(len(confs) - 1):
            assert confs[i] < confs[i + 1]

    def test_confidence_below_prior(self) -> None:
        """Less than prior observations returns 0.0."""
        assert compute_confidence(0.5, 0.5) == 0.0


# ---------------------------------------------------------------------------
# 3. compute_decay_weight tests
# ---------------------------------------------------------------------------


class TestComputeDecayWeight:
    """Test exponential time-decay weight."""

    def test_zero_age_is_full_weight(self) -> None:
        """age=0 → weight=1.0."""
        assert compute_decay_weight(0.0) == 1.0

    def test_one_half_life(self) -> None:
        """age=half_life → weight=0.5."""
        weight = compute_decay_weight(DEFAULT_HALF_LIFE_SECONDS)
        assert weight == pytest.approx(0.5, abs=1e-6)

    def test_two_half_lives(self) -> None:
        """age=2*half_life → weight=0.25."""
        weight = compute_decay_weight(2 * DEFAULT_HALF_LIFE_SECONDS)
        assert weight == pytest.approx(0.25, abs=1e-6)

    def test_custom_half_life(self) -> None:
        """Custom half-life of 1 hour."""
        weight = compute_decay_weight(3600.0, half_life_seconds=3600.0)
        assert weight == pytest.approx(0.5, abs=1e-6)

    def test_negative_age_is_full_weight(self) -> None:
        """Negative age (future) → weight=1.0."""
        assert compute_decay_weight(-100.0) == 1.0

    def test_zero_half_life_is_zero_weight(self) -> None:
        """Zero half-life → weight=0.0 (instant decay)."""
        assert compute_decay_weight(100.0, half_life_seconds=0.0) == 0.0

    def test_very_old_event_near_zero(self) -> None:
        """Very old event (10 half-lives) → near zero."""
        weight = compute_decay_weight(10 * DEFAULT_HALF_LIFE_SECONDS)
        assert weight < 0.001

    def test_decay_monotonically_decreases(self) -> None:
        """Weight decreases with age."""
        ages = [0.0, 3600.0, 86400.0, 604800.0, DEFAULT_HALF_LIFE_SECONDS]
        weights = [compute_decay_weight(a) for a in ages]
        for i in range(len(weights) - 1):
            assert weights[i] > weights[i + 1]


# ---------------------------------------------------------------------------
# 4. compute_composite_score tests
# ---------------------------------------------------------------------------


class TestComputeCompositeScore:
    """Test weighted composite score from per-dimension Beta parameters."""

    def test_all_priors(self) -> None:
        """All dimensions at prior → composite = (0.5, 0.0)."""
        dims = {
            "reliability": (1.0, 1.0),
            "quality": (1.0, 1.0),
            "timeliness": (1.0, 1.0),
            "fairness": (1.0, 1.0),
        }
        score, conf = compute_composite_score(dims)
        assert score == pytest.approx(0.5, abs=1e-6)
        assert conf == pytest.approx(0.0, abs=1e-6)

    def test_all_perfect(self) -> None:
        """All dimensions strongly positive → high composite score."""
        dims = {
            "reliability": (101.0, 1.0),
            "quality": (101.0, 1.0),
            "timeliness": (101.0, 1.0),
            "fairness": (101.0, 1.0),
        }
        score, conf = compute_composite_score(dims)
        assert score > 0.95
        assert conf > 0.5

    def test_mixed_dimensions(self) -> None:
        """Mix of good and bad dimensions."""
        dims = {
            "reliability": (11.0, 1.0),  # score ≈ 0.917
            "quality": (1.0, 11.0),  # score ≈ 0.083
            "timeliness": (6.0, 6.0),  # score = 0.5
            "fairness": (6.0, 6.0),  # score = 0.5
        }
        score, conf = compute_composite_score(dims)
        # Weighted: 0.3*0.917 + 0.3*0.083 + 0.2*0.5 + 0.2*0.5 = 0.275 + 0.025 + 0.1 + 0.1 = 0.5
        assert score == pytest.approx(0.5, abs=0.01)

    def test_custom_weights(self) -> None:
        """Custom dimension weights."""
        dims = {
            "reliability": (11.0, 1.0),  # score ≈ 0.917
            "quality": (1.0, 11.0),  # score ≈ 0.083
        }
        weights = {"reliability": 0.8, "quality": 0.2}
        score, _conf = compute_composite_score(dims, weights=weights)
        expected = 0.8 * (11.0 / 12.0) + 0.2 * (1.0 / 12.0)
        assert score == pytest.approx(expected, abs=1e-6)

    def test_single_dimension(self) -> None:
        """Single dimension present → full weight on it."""
        dims = {"reliability": (11.0, 1.0)}
        score, conf = compute_composite_score(dims)
        assert score == pytest.approx(11.0 / 12.0, abs=1e-6)
        assert conf > 0

    def test_empty_dimensions(self) -> None:
        """No dimensions → (0.5, 0.0)."""
        score, conf = compute_composite_score({})
        assert score == 0.5
        assert conf == 0.0

    def test_unknown_dimensions_equal_weight(self) -> None:
        """Dimensions not in default weights get equal weight."""
        dims = {
            "custom_dim_a": (11.0, 1.0),
            "custom_dim_b": (1.0, 11.0),
        }
        score, _conf = compute_composite_score(dims)
        # Equal weight: 0.5 * 0.917 + 0.5 * 0.083 ≈ 0.5
        assert score == pytest.approx(0.5, abs=0.01)

    def test_partial_dimensions(self) -> None:
        """Only some default dimensions provided → weights renormalized."""
        dims = {
            "reliability": (11.0, 1.0),  # default weight 0.3
            "quality": (1.0, 11.0),  # default weight 0.3
        }
        score, _conf = compute_composite_score(dims)
        # Weights renormalized: 0.3/(0.3+0.3) = 0.5 each
        expected = 0.5 * (11.0 / 12.0) + 0.5 * (1.0 / 12.0)
        assert score == pytest.approx(expected, abs=1e-6)
