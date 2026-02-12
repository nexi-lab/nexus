"""Unit tests for ReputationService (Issue #1356).

Tests against in-memory SQLite. Covers:
1. Submit feedback → creates event + updates score
2. Query reputation → returns correct composite score
3. Edge cases: self-rating (ValueError), duplicate (DuplicateFeedbackError),
   out-of-range (ValueError)
4. Score incremental update: verify alpha/beta after N feedbacks
5. Cache behavior: TTLCache hit/miss/invalidation
6. Leaderboard ordering
7. Feedback retrieval for exchange
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.reputation_service import DuplicateFeedbackError, ReputationService
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """In-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Session factory for tests."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def reputation_service(session_factory):
    """ReputationService instance for testing."""
    return ReputationService(
        session_factory=session_factory,
        cache_maxsize=100,
        cache_ttl=60,
    )


# ---------------------------------------------------------------------------
# 1. Submit feedback
# ---------------------------------------------------------------------------


class TestSubmitFeedback:
    """Test feedback submission creates events and updates scores."""

    def test_submit_positive_feedback(self, reputation_service):
        """Positive feedback creates event and updates score."""
        event = reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-1",
            zone_id="default",
            outcome="positive",
        )

        assert event.rater_agent_id == "agent-a"
        assert event.rated_agent_id == "agent-b"
        assert event.exchange_id == "exchange-1"
        assert event.event_type == "feedback"
        assert event.outcome == "positive"
        assert event.record_hash  # non-empty hash

    def test_submit_negative_feedback(self, reputation_service):
        """Negative feedback creates event."""
        event = reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-2",
            zone_id="default",
            outcome="negative",
        )
        assert event.outcome == "negative"

    def test_submit_feedback_with_dimension_scores(self, reputation_service):
        """Feedback with per-dimension scores."""
        event = reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-3",
            zone_id="default",
            outcome="positive",
            reliability_score=0.9,
            quality_score=0.8,
            timeliness_score=0.7,
            fairness_score=0.6,
        )
        assert event.reliability_score == 0.9
        assert event.quality_score == 0.8
        assert event.timeliness_score == 0.7
        assert event.fairness_score == 0.6

    def test_submit_feedback_creates_score(self, reputation_service):
        """First feedback creates materialized score record."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-4",
            zone_id="default",
            outcome="positive",
        )

        score = reputation_service.get_reputation("agent-b")
        assert score is not None
        assert score.agent_id == "agent-b"
        assert score.total_interactions == 1
        assert score.positive_interactions == 1
        assert score.composite_score > 0.5  # positive shifts score up

    def test_submit_feedback_mixed_outcome(self, reputation_service):
        """Mixed outcome feedback."""
        event = reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-5",
            zone_id="default",
            outcome="mixed",
        )
        assert event.outcome == "mixed"


# ---------------------------------------------------------------------------
# 2. Query reputation
# ---------------------------------------------------------------------------


class TestGetReputation:
    """Test reputation score queries."""

    def test_get_nonexistent_reputation(self, reputation_service):
        """Agent with no feedback returns None."""
        assert reputation_service.get_reputation("unknown-agent") is None

    def test_get_reputation_after_feedback(self, reputation_service):
        """Score is returned after feedback."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-6",
            zone_id="default",
            outcome="positive",
        )

        score = reputation_service.get_reputation("agent-b")
        assert score is not None
        assert score.composite_score > 0.5
        assert score.composite_confidence > 0.0

    def test_cache_returns_same_result(self, reputation_service):
        """Cached result is returned on second query."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-7",
            zone_id="default",
            outcome="positive",
        )

        score1 = reputation_service.get_reputation("agent-b")
        score2 = reputation_service.get_reputation("agent-b")
        assert score1 == score2


# ---------------------------------------------------------------------------
# 3. Edge cases / validation
# ---------------------------------------------------------------------------


class TestFeedbackValidation:
    """Test input validation and edge cases."""

    def test_self_rating_rejected(self, reputation_service):
        """Self-rating raises ValueError."""
        with pytest.raises(ValueError, match="Self-rating"):
            reputation_service.submit_feedback(
                rater_agent_id="agent-a",
                rated_agent_id="agent-a",
                exchange_id="exchange-8",
                zone_id="default",
                outcome="positive",
            )

    def test_duplicate_feedback_rejected(self, reputation_service):
        """Duplicate feedback raises DuplicateFeedbackError."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-9",
            zone_id="default",
            outcome="positive",
        )

        with pytest.raises(DuplicateFeedbackError):
            reputation_service.submit_feedback(
                rater_agent_id="agent-a",
                rated_agent_id="agent-b",
                exchange_id="exchange-9",
                zone_id="default",
                outcome="negative",
            )

    def test_invalid_outcome_rejected(self, reputation_service):
        """Invalid outcome raises ValueError."""
        with pytest.raises(ValueError, match="Invalid outcome"):
            reputation_service.submit_feedback(
                rater_agent_id="agent-a",
                rated_agent_id="agent-b",
                exchange_id="exchange-10",
                zone_id="default",
                outcome="invalid_outcome",
            )

    def test_score_out_of_range_rejected(self, reputation_service):
        """Score > 1.0 raises ValueError."""
        with pytest.raises(ValueError, match="must be between"):
            reputation_service.submit_feedback(
                rater_agent_id="agent-a",
                rated_agent_id="agent-b",
                exchange_id="exchange-11",
                zone_id="default",
                outcome="positive",
                reliability_score=1.5,
            )

    def test_score_negative_rejected(self, reputation_service):
        """Score < 0.0 raises ValueError."""
        with pytest.raises(ValueError, match="must be between"):
            reputation_service.submit_feedback(
                rater_agent_id="agent-a",
                rated_agent_id="agent-b",
                exchange_id="exchange-12",
                zone_id="default",
                outcome="positive",
                quality_score=-0.1,
            )


# ---------------------------------------------------------------------------
# 4. Score incremental update
# ---------------------------------------------------------------------------


class TestScoreUpdate:
    """Test incremental score updates after multiple feedbacks."""

    def test_multiple_positive_increases_score(self, reputation_service):
        """Multiple positive feedbacks increase score above 0.5."""
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-{i}",
                rated_agent_id="agent-b",
                exchange_id=f"exchange-multi-{i}",
                zone_id="default",
                outcome="positive",
            )

        score = reputation_service.get_reputation("agent-b")
        assert score is not None
        assert score.total_interactions == 5
        assert score.positive_interactions == 5
        assert score.composite_score > 0.7

    def test_mixed_feedbacks_moderate_score(self, reputation_service):
        """Mix of positive and negative feedbacks → near 0.5."""
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-pos-{i}",
                rated_agent_id="agent-b",
                exchange_id=f"exchange-mix-pos-{i}",
                zone_id="default",
                outcome="positive",
            )
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-neg-{i}",
                rated_agent_id="agent-b",
                exchange_id=f"exchange-mix-neg-{i}",
                zone_id="default",
                outcome="negative",
            )

        score = reputation_service.get_reputation("agent-b")
        assert score is not None
        assert score.total_interactions == 10
        assert 0.4 < score.composite_score < 0.6

    def test_dimension_scores_update_alpha_beta(self, reputation_service):
        """Per-dimension scores update alpha/beta correctly."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-dim-1",
            zone_id="default",
            outcome="positive",
            reliability_score=1.0,  # full positive → alpha += 1.0, beta += 0.0
            quality_score=0.0,  # full negative → alpha += 0.0, beta += 1.0
        )

        score = reputation_service.get_reputation("agent-b")
        assert score is not None
        # reliability: prior(1,1) + (1.0, 0.0) = (2.0, 1.0) → score ≈ 0.667
        assert score.reliability_alpha == pytest.approx(2.0, abs=0.01)
        assert score.reliability_beta == pytest.approx(1.0, abs=0.01)
        # quality: prior(1,1) + (0.0, 1.0) = (1.0, 2.0) → score ≈ 0.333
        assert score.quality_alpha == pytest.approx(1.0, abs=0.01)
        assert score.quality_beta == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# 5. Cache invalidation
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    """Test cache behavior on feedback submission."""

    def test_cache_invalidated_on_new_feedback(self, reputation_service):
        """Cache is invalidated when new feedback is submitted."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-cache-1",
            zone_id="default",
            outcome="positive",
        )

        # First query populates cache
        score1 = reputation_service.get_reputation("agent-b")

        # Submit more feedback (invalidates cache)
        reputation_service.submit_feedback(
            rater_agent_id="agent-c",
            rated_agent_id="agent-b",
            exchange_id="exchange-cache-2",
            zone_id="default",
            outcome="positive",
        )

        # Second query should reflect the updated score
        score2 = reputation_service.get_reputation("agent-b")
        assert score2 is not None
        assert score1 is not None
        assert score2.total_interactions == 2
        assert score2.composite_score >= score1.composite_score


# ---------------------------------------------------------------------------
# 6. Leaderboard
# ---------------------------------------------------------------------------


class TestLeaderboard:
    """Test zone leaderboard queries."""

    def test_leaderboard_ordering(self, reputation_service):
        """Leaderboard returns agents ordered by composite score descending."""
        # Agent-high: 5 positive
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-h-{i}",
                rated_agent_id="agent-high",
                exchange_id=f"exchange-lb-h-{i}",
                zone_id="zone-lb",
                outcome="positive",
            )

        # Agent-low: 5 negative
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-l-{i}",
                rated_agent_id="agent-low",
                exchange_id=f"exchange-lb-l-{i}",
                zone_id="zone-lb",
                outcome="negative",
            )

        board = reputation_service.get_leaderboard("zone-lb")
        assert len(board) == 2
        assert board[0].agent_id == "agent-high"
        assert board[1].agent_id == "agent-low"
        assert board[0].composite_score > board[1].composite_score

    def test_leaderboard_empty_zone(self, reputation_service):
        """Leaderboard for zone with no agents returns empty list."""
        board = reputation_service.get_leaderboard("empty-zone")
        assert board == []

    def test_leaderboard_limit(self, reputation_service):
        """Leaderboard respects limit parameter."""
        for i in range(5):
            reputation_service.submit_feedback(
                rater_agent_id=f"rater-lim-{i}",
                rated_agent_id=f"agent-lim-{i}",
                exchange_id=f"exchange-lim-{i}",
                zone_id="zone-lim",
                outcome="positive",
            )

        board = reputation_service.get_leaderboard("zone-lim", limit=2)
        assert len(board) == 2


# ---------------------------------------------------------------------------
# 7. Feedback retrieval
# ---------------------------------------------------------------------------


class TestGetFeedbackForExchange:
    """Test feedback retrieval for an exchange."""

    def test_get_feedback_for_exchange(self, reputation_service):
        """Retrieve all feedback for a specific exchange."""
        reputation_service.submit_feedback(
            rater_agent_id="agent-a",
            rated_agent_id="agent-b",
            exchange_id="exchange-fb-1",
            zone_id="default",
            outcome="positive",
        )
        reputation_service.submit_feedback(
            rater_agent_id="agent-b",
            rated_agent_id="agent-a",
            exchange_id="exchange-fb-1",
            zone_id="default",
            outcome="negative",
        )

        feedback = reputation_service.get_feedback_for_exchange("exchange-fb-1")
        assert len(feedback) == 2
        agent_ids = {f.rater_agent_id for f in feedback}
        assert agent_ids == {"agent-a", "agent-b"}

    def test_get_feedback_empty(self, reputation_service):
        """No feedback for exchange returns empty list."""
        feedback = reputation_service.get_feedback_for_exchange("no-such-exchange")
        assert feedback == []
