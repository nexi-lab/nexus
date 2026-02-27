"""Unit tests for the trust-score endpoint (#1619).

Tests the GET /api/v2/agents/{agent_id}/trust-score endpoint:
1. Returns composite score
2. Returns per-dimension score
3. Returns 404 for unknown agent
4. Returns 400 for invalid dimension
"""

from dataclasses import dataclass
from datetime import datetime

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.routers.reputation import (
    _VALID_DIMENSIONS,
    TrustScoreResponse,
    get_trust_score,
)


@dataclass(frozen=True)
class FakeReputationScore:
    """Minimal ReputationScore for endpoint testing."""

    agent_id: str = "agent-1"
    context: str = "general"
    window: str = "all_time"
    composite_score: float = 0.75
    composite_confidence: float = 0.82
    reliability_alpha: float = 8.0
    reliability_beta: float = 2.0
    quality_alpha: float = 6.0
    quality_beta: float = 1.5
    timeliness_alpha: float = 4.0
    timeliness_beta: float = 1.0
    fairness_alpha: float = 3.0
    fairness_beta: float = 2.0
    total_interactions: int = 15
    positive_interactions: int = 12
    negative_interactions: int = 3
    disputed_interactions: int = 0
    global_trust_score: float | None = None
    updated_at: datetime = datetime(2025, 1, 1)
    zone_id: str = ROOT_ZONE_ID


class FakeReputationService:
    """Fake ReputationService for testing."""

    def __init__(self, scores: dict[str, FakeReputationScore | None] | None = None):
        self._scores = scores or {}

    def get_reputation(self, agent_id: str, **kwargs) -> FakeReputationScore | None:
        return self._scores.get(agent_id)


class TestGetTrustScore:
    """Tests for the get_trust_score endpoint function."""

    def test_get_trust_score_composite(self):
        """Returns composite score and confidence."""
        score = FakeReputationScore(composite_score=0.85, composite_confidence=0.9)
        rep_service = FakeReputationService({"agent-1": score})
        deps = (rep_service, None, {})

        result = get_trust_score(agent_id="agent-1", dimension="composite", deps=deps)

        assert isinstance(result, TrustScoreResponse)
        assert result.agent_id == "agent-1"
        assert result.dimension == "composite"
        assert result.score == 0.85
        assert result.confidence == 0.9

    def test_get_trust_score_reliability_dimension(self):
        """Returns per-dimension score computed from alpha/beta."""
        score = FakeReputationScore(reliability_alpha=8.0, reliability_beta=2.0)
        rep_service = FakeReputationService({"agent-1": score})
        deps = (rep_service, None, {})

        result = get_trust_score(agent_id="agent-1", dimension="reliability", deps=deps)

        assert result.dimension == "reliability"
        # Expected: alpha / (alpha + beta) = 8.0 / 10.0 = 0.8
        assert abs(result.score - 0.8) < 0.001
        assert result.confidence > 0.0

    def test_get_trust_score_quality_dimension(self):
        """Returns quality dimension score."""
        score = FakeReputationScore(quality_alpha=6.0, quality_beta=1.5)
        rep_service = FakeReputationService({"agent-1": score})
        deps = (rep_service, None, {})

        result = get_trust_score(agent_id="agent-1", dimension="quality", deps=deps)

        assert result.dimension == "quality"
        # Expected: 6.0 / 7.5 = 0.8
        assert abs(result.score - 0.8) < 0.001

    def test_get_trust_score_not_found(self):
        """Returns 404 for unknown agent."""
        rep_service = FakeReputationService({})
        deps = (rep_service, None, {})

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            get_trust_score(agent_id="unknown-agent", dimension="composite", deps=deps)

        assert exc_info.value.status_code == 404

    def test_get_trust_score_invalid_dimension(self):
        """Returns 400 for invalid dimension name."""
        score = FakeReputationScore()
        rep_service = FakeReputationService({"agent-1": score})
        deps = (rep_service, None, {})

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            get_trust_score(agent_id="agent-1", dimension="bogus", deps=deps)

        assert exc_info.value.status_code == 400
        assert "Invalid dimension" in str(exc_info.value.detail)

    def test_valid_dimensions_set(self):
        """Verify the set of valid dimensions."""
        assert {
            "composite",
            "reliability",
            "quality",
            "timeliness",
            "fairness",
        } == _VALID_DIMENSIONS

    def test_get_trust_score_with_zone_id(self):
        """Zone ID is passed through to response."""
        score = FakeReputationScore()
        rep_service = FakeReputationService({"agent-1": score})
        deps = (rep_service, None, {})

        result = get_trust_score(
            agent_id="agent-1", dimension="composite", zone_id="my-zone", deps=deps
        )

        assert result.zone_id == "my-zone"
