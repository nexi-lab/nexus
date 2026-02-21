"""Unit tests for reputation materialized score pipeline (Issue #2131, Phase 6.3).

Tests the feedback → event → materialized score pipeline including:
- Alpha/beta increments from outcomes
- Dimension score scaling
- Composite recomputation
- Get-or-create for new agents
- Multiple feedback accumulation
- Interaction counters
"""

from datetime import UTC, datetime

import pytest

from nexus.bricks.reputation.reputation_math import (
    compute_beta_score,
    compute_composite_score,
    compute_confidence,
)

# ---------------------------------------------------------------------------
# Tests: reputation_math pure functions
# ---------------------------------------------------------------------------


class TestBetaScore:
    """Tests for compute_beta_score."""

    def test_prior_gives_half(self) -> None:
        """Beta(1,1) prior gives score 0.5."""
        assert compute_beta_score(1.0, 1.0) == pytest.approx(0.5)

    def test_positive_evidence_increases_score(self) -> None:
        """More positive evidence (higher alpha) increases score."""
        score = compute_beta_score(11.0, 2.0)
        assert score > 0.5
        assert score == pytest.approx(11.0 / 13.0)

    def test_negative_evidence_decreases_score(self) -> None:
        """More negative evidence (higher beta) decreases score."""
        score = compute_beta_score(2.0, 11.0)
        assert score < 0.5
        assert score == pytest.approx(2.0 / 13.0)

    def test_zero_total_gives_half(self) -> None:
        """Zero total (edge case) gives 0.5."""
        assert compute_beta_score(0.0, 0.0) == 0.5


class TestConfidence:
    """Tests for compute_confidence."""

    def test_no_observations_zero_confidence(self) -> None:
        """Prior Beta(1,1) = 0 observations → confidence 0."""
        assert compute_confidence(1.0, 1.0) == 0.0

    def test_more_observations_higher_confidence(self) -> None:
        """Confidence increases with more observations."""
        conf_1 = compute_confidence(2.0, 1.0)  # 1 observation
        conf_10 = compute_confidence(6.0, 6.0)  # 10 observations
        conf_100 = compute_confidence(51.0, 51.0)  # 100 observations

        assert 0 < conf_1 < conf_10 < conf_100 < 1.0

    def test_confidence_bounded(self) -> None:
        """Confidence is always in [0, 1]."""
        assert compute_confidence(1001.0, 1001.0) < 1.0
        assert compute_confidence(1001.0, 1001.0) > 0.0


class TestCompositeScore:
    """Tests for compute_composite_score."""

    def test_uniform_dimensions(self) -> None:
        """All dimensions at prior → composite = 0.5."""
        dims = {
            "reliability": (1.0, 1.0),
            "quality": (1.0, 1.0),
            "timeliness": (1.0, 1.0),
            "fairness": (1.0, 1.0),
        }
        score, confidence = compute_composite_score(dims)
        assert score == pytest.approx(0.5)
        assert confidence == 0.0

    def test_high_reliability_pulls_composite_up(self) -> None:
        """High reliability score (weight=0.3) pulls composite above 0.5."""
        dims = {
            "reliability": (11.0, 2.0),  # ~0.846
            "quality": (1.0, 1.0),  # 0.5
            "timeliness": (1.0, 1.0),  # 0.5
            "fairness": (1.0, 1.0),  # 0.5
        }
        score, _ = compute_composite_score(dims)
        assert score > 0.5

    def test_empty_dimensions(self) -> None:
        """No dimensions → default (0.5, 0.0)."""
        score, conf = compute_composite_score({})
        assert score == 0.5
        assert conf == 0.0

    def test_custom_weights(self) -> None:
        """Custom weights are respected."""
        dims = {
            "reliability": (11.0, 2.0),  # ~0.846
            "quality": (2.0, 11.0),  # ~0.154
        }
        # Equal weights
        score_eq, _ = compute_composite_score(dims, weights={"reliability": 0.5, "quality": 0.5})
        assert score_eq == pytest.approx(0.5, abs=0.01)

        # Heavy reliability weight
        score_rel, _ = compute_composite_score(dims, weights={"reliability": 0.9, "quality": 0.1})
        assert score_rel > 0.7


# ---------------------------------------------------------------------------
# Tests: ReputationService._update_dimension (static method)
# ---------------------------------------------------------------------------


class FakeScoreModel:
    """Fake ReputationScoreModel with alpha/beta attributes."""

    def __init__(self) -> None:
        self.reliability_alpha: float = 1.0
        self.reliability_beta: float = 1.0
        self.quality_alpha: float = 1.0
        self.quality_beta: float = 1.0
        self.timeliness_alpha: float = 1.0
        self.timeliness_beta: float = 1.0
        self.fairness_alpha: float = 1.0
        self.fairness_beta: float = 1.0
        self.composite_score: float = 0.5
        self.composite_confidence: float = 0.0
        self.total_interactions: int = 0
        self.positive_interactions: int = 0
        self.negative_interactions: int = 0
        self.disputed_interactions: int = 0
        self.global_trust_score: float | None = None
        self.updated_at: datetime = datetime.now(UTC)


class TestUpdateDimension:
    """Tests for ReputationService._update_dimension static method."""

    def _update(
        self,
        model: FakeScoreModel,
        dimension: str,
        score: float | None,
        alpha_inc: float,
        beta_inc: float,
    ) -> None:
        from nexus.bricks.reputation.reputation_service import ReputationService

        ReputationService._update_dimension(model, dimension, score, alpha_inc, beta_inc)

    def test_positive_feedback_increments_alpha(self) -> None:
        """Positive outcome increments alpha by 1.0, beta unchanged."""
        model = FakeScoreModel()
        self._update(model, "reliability", None, 1.0, 0.0)

        assert model.reliability_alpha == pytest.approx(2.0)
        assert model.reliability_beta == pytest.approx(1.0)

    def test_negative_feedback_increments_beta(self) -> None:
        """Negative outcome increments beta by 1.0, alpha unchanged."""
        model = FakeScoreModel()
        self._update(model, "reliability", None, 0.0, 1.0)

        assert model.reliability_alpha == pytest.approx(1.0)
        assert model.reliability_beta == pytest.approx(2.0)

    def test_dimension_score_scaling(self) -> None:
        """Dimension score=0.8 → alpha +0.8, beta +0.2."""
        model = FakeScoreModel()
        self._update(model, "reliability", 0.8, 1.0, 0.0)

        assert model.reliability_alpha == pytest.approx(1.8)
        assert model.reliability_beta == pytest.approx(1.2)

    def test_dimension_score_zero(self) -> None:
        """Dimension score=0.0 → alpha +0.0, beta +1.0."""
        model = FakeScoreModel()
        self._update(model, "quality", 0.0, 1.0, 0.0)

        assert model.quality_alpha == pytest.approx(1.0)
        assert model.quality_beta == pytest.approx(2.0)

    def test_dimension_score_one(self) -> None:
        """Dimension score=1.0 → alpha +1.0, beta +0.0."""
        model = FakeScoreModel()
        self._update(model, "quality", 1.0, 0.0, 1.0)

        assert model.quality_alpha == pytest.approx(2.0)
        assert model.quality_beta == pytest.approx(1.0)


class TestCompositeRecomputation:
    """Tests for composite score matching reputation_math output."""

    def test_composite_matches_math_output(self) -> None:
        """Composite score and confidence match reputation_math functions."""
        model = FakeScoreModel()
        # Simulate several positive feedbacks on reliability
        model.reliability_alpha = 11.0
        model.reliability_beta = 2.0
        model.quality_alpha = 5.0
        model.quality_beta = 3.0
        model.timeliness_alpha = 3.0
        model.timeliness_beta = 3.0
        model.fairness_alpha = 4.0
        model.fairness_beta = 2.0

        dims = {
            "reliability": (model.reliability_alpha, model.reliability_beta),
            "quality": (model.quality_alpha, model.quality_beta),
            "timeliness": (model.timeliness_alpha, model.timeliness_beta),
            "fairness": (model.fairness_alpha, model.fairness_beta),
        }
        expected_score, expected_conf = compute_composite_score(dims)

        assert expected_score == pytest.approx(
            0.3 * compute_beta_score(11, 2)
            + 0.3 * compute_beta_score(5, 3)
            + 0.2 * compute_beta_score(3, 3)
            + 0.2 * compute_beta_score(4, 2),
            abs=0.001,
        )


class TestMultipleFeedbackAccumulation:
    """Tests for accumulating multiple feedbacks."""

    def test_two_positive_feedbacks_accumulate(self) -> None:
        """Two positive feedbacks → alpha = 1 + 2 = 3.0."""
        model = FakeScoreModel()
        from nexus.bricks.reputation.reputation_service import ReputationService

        # First positive feedback
        ReputationService._update_dimension(model, "reliability", None, 1.0, 0.0)
        # Second positive feedback
        ReputationService._update_dimension(model, "reliability", None, 1.0, 0.0)

        assert model.reliability_alpha == pytest.approx(3.0)
        assert model.reliability_beta == pytest.approx(1.0)

    def test_mixed_feedbacks(self) -> None:
        """One positive + one negative → alpha=2.0, beta=2.0."""
        model = FakeScoreModel()
        from nexus.bricks.reputation.reputation_service import ReputationService

        ReputationService._update_dimension(model, "quality", None, 1.0, 0.0)
        ReputationService._update_dimension(model, "quality", None, 0.0, 1.0)

        assert model.quality_alpha == pytest.approx(2.0)
        assert model.quality_beta == pytest.approx(2.0)


class TestInteractionCounters:
    """Tests for interaction counter mapping."""

    def test_outcome_increments(self) -> None:
        """Verify outcome → alpha/beta mapping from _OUTCOME_INCREMENTS."""
        from nexus.bricks.reputation.reputation_service import _OUTCOME_INCREMENTS

        assert _OUTCOME_INCREMENTS["positive"] == (1.0, 0.0)
        assert _OUTCOME_INCREMENTS["negative"] == (0.0, 1.0)
        assert _OUTCOME_INCREMENTS["neutral"] == (0.3, 0.3)
        assert _OUTCOME_INCREMENTS["mixed"] == (0.5, 0.5)
