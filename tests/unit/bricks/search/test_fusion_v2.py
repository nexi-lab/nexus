"""Unit tests for QMD-inspired multi-source fusion (rrf_multi_fusion, position_aware_blend).

Tests cover:
- rrf_multi_fusion: N-way RRF with per-source weights, backward compat, top-rank bonus
- position_aware_blend: tiered blending, edge cases, custom tiers
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.fusion import position_aware_blend, rrf_multi_fusion

# =============================================================================
# Helpers
# =============================================================================


def _make_results(ids: list[str], scores: list[float] | None = None) -> list[dict]:
    """Build a list of result dicts with chunk_id and score."""
    if scores is None:
        scores = [1.0 / (i + 1) for i in range(len(ids))]
    return [
        {"chunk_id": cid, "score": score, "chunk_text": f"text for {cid}"}
        for cid, score in zip(ids, scores, strict=False)
    ]


# =============================================================================
# rrf_multi_fusion tests
# =============================================================================


class TestRRFMultiFusion:
    """Tests for rrf_multi_fusion()."""

    def test_rrf_multi_fusion_with_weights(self) -> None:
        """2.0x vs 1.0x weight produces different ordering than equal weights."""
        source_a = _make_results(["a1", "a2", "a3"])
        source_b = _make_results(["b1", "a1", "a2"])

        # Weighted: source_a has 2.0x weight, source_b has 1.0x
        weighted_results = rrf_multi_fusion(
            [("src_a", source_a, 2.0), ("src_b", source_b, 1.0)],
            k=60,
            limit=5,
        )

        # Equal weights: both 1.0x
        equal_results = rrf_multi_fusion(
            [("src_a", source_a, 1.0), ("src_b", source_b, 1.0)],
            k=60,
            limit=5,
        )

        # With higher weight on source_a, "a1" (rank 1 in source_a) should
        # have a higher score than when weights are equal.
        weighted_scores = {r["chunk_id"]: r["score"] for r in weighted_results}
        equal_scores = {r["chunk_id"]: r["score"] for r in equal_results}

        # a1 appears in both sources. With source_a weighted 2.0:
        # weighted contribution from source_a = 2.0 / (60+1) vs 1.0 / (60+1) equal
        assert weighted_scores["a1"] > equal_scores["a1"]

        # b1 only appears in source_b (weight 1.0 in both cases), should be same
        assert pytest.approx(weighted_scores["b1"]) == equal_scores["b1"]

    def test_rrf_multi_fusion_backward_compat(self) -> None:
        """2-element tuples (no weight) still work with implicit weight 1.0."""
        source_a = _make_results(["a1", "a2"])
        source_b = _make_results(["b1", "a1"])

        # 2-element tuples (backward compatible)
        results_2elem = rrf_multi_fusion(
            [("src_a", source_a), ("src_b", source_b)],
            k=60,
            limit=5,
        )

        # 3-element tuples with explicit weight 1.0 (should be identical)
        results_3elem = rrf_multi_fusion(
            [("src_a", source_a, 1.0), ("src_b", source_b, 1.0)],
            k=60,
            limit=5,
        )

        # Scores should be identical
        scores_2 = {r["chunk_id"]: r["score"] for r in results_2elem}
        scores_3 = {r["chunk_id"]: r["score"] for r in results_3elem}

        for cid in scores_2:
            assert pytest.approx(scores_2[cid]) == scores_3[cid]

    def test_rrf_multi_fusion_top_rank_bonus(self) -> None:
        """Rank 1 gets +0.05 bonus, ranks 2-3 get +0.02 bonus."""
        source_a = _make_results(["a1", "a2", "a3", "a4"])

        # Without top_rank_bonus
        results_no_bonus = rrf_multi_fusion(
            [("src_a", source_a, 1.0)],
            k=60,
            limit=10,
            top_rank_bonus=False,
        )

        # With top_rank_bonus
        results_bonus = rrf_multi_fusion(
            [("src_a", source_a, 1.0)],
            k=60,
            limit=10,
            top_rank_bonus=True,
        )

        scores_no = {r["chunk_id"]: r["score"] for r in results_no_bonus}
        scores_yes = {r["chunk_id"]: r["score"] for r in results_bonus}

        # Rank 1 ("a1") should get +0.05 bonus
        assert pytest.approx(scores_yes["a1"] - scores_no["a1"], abs=1e-9) == 0.05

        # Rank 2 ("a2") should get +0.02 bonus
        assert pytest.approx(scores_yes["a2"] - scores_no["a2"], abs=1e-9) == 0.02

        # Rank 3 ("a3") should get +0.02 bonus
        assert pytest.approx(scores_yes["a3"] - scores_no["a3"], abs=1e-9) == 0.02

        # Rank 4 ("a4") should get no bonus
        assert pytest.approx(scores_yes["a4"], abs=1e-9) == scores_no["a4"]


# =============================================================================
# position_aware_blend tests
# =============================================================================


class TestPositionAwareBlend:
    """Tests for position_aware_blend()."""

    def test_position_aware_blend_tiers(self) -> None:
        """Verify default tiers: 75/25 (rank 1-3), 60/40 (rank 4-10), 40/60 (rank 11+)."""
        # Build 12 fused results so we cover all 3 tiers
        fused = [
            {"chunk_id": f"c{i}", "score": 1.0 - i * 0.05, "chunk_text": f"text {i}"}
            for i in range(12)
        ]

        # Reranker gives uniform 0.8 to all
        reranker_scores = {f"c{i}": 0.8 for i in range(12)}

        blended = position_aware_blend(fused, reranker_scores)

        # Check tier 1 (ranks 1-3): 0.75 * retrieval + 0.25 * reranker
        # c0: retrieval=1.0, reranker=0.8 => 0.75*1.0 + 0.25*0.8 = 0.95
        c0 = next(r for r in blended if r["chunk_id"] == "c0")
        assert pytest.approx(c0["original_retrieval_score"]) == 1.0
        expected_c0 = 0.75 * 1.0 + 0.25 * 0.8
        assert pytest.approx(c0["score"], abs=1e-9) == expected_c0

        # Check tier 2 (ranks 4-10): 0.60 * retrieval + 0.40 * reranker
        # c5: retrieval=0.75, reranker=0.8 => 0.60*0.75 + 0.40*0.8 = 0.77
        c5 = next(r for r in blended if r["chunk_id"] == "c5")
        expected_c5 = 0.60 * c5["original_retrieval_score"] + 0.40 * 0.8
        assert pytest.approx(c5["score"], abs=1e-9) == expected_c5

        # Check tier 3 (ranks 11+): 0.40 * retrieval + 0.60 * reranker
        # c11: retrieval=0.45, reranker=0.8 => 0.40*0.45 + 0.60*0.8 = 0.66
        c11 = next(r for r in blended if r["chunk_id"] == "c11")
        expected_c11 = 0.40 * c11["original_retrieval_score"] + 0.60 * 0.8
        assert pytest.approx(c11["score"], abs=1e-9) == expected_c11

    def test_position_aware_blend_empty(self) -> None:
        """Empty results returns empty list."""
        result = position_aware_blend([], {})
        assert result == []

    def test_position_aware_blend_single_result(self) -> None:
        """Single result uses tier 1 weights (0.75/0.25)."""
        fused = [{"chunk_id": "only", "score": 0.9, "chunk_text": "single result"}]
        reranker_scores = {"only": 0.6}

        blended = position_aware_blend(fused, reranker_scores)
        assert len(blended) == 1

        expected = 0.75 * 0.9 + 0.25 * 0.6
        assert pytest.approx(blended[0]["score"], abs=1e-9) == expected
        assert blended[0]["original_retrieval_score"] == 0.9
        assert blended[0]["reranker_score"] == 0.6

    def test_position_aware_blend_custom_tiers(self) -> None:
        """Custom tier configuration overrides defaults."""
        fused = [
            {"chunk_id": "c0", "score": 1.0, "chunk_text": "first"},
            {"chunk_id": "c1", "score": 0.8, "chunk_text": "second"},
            {"chunk_id": "c2", "score": 0.6, "chunk_text": "third"},
        ]
        reranker_scores = {"c0": 0.5, "c1": 0.9, "c2": 0.7}

        # Custom tiers: rank 1 is 50/50, rank 2+ is 20/80
        custom_tiers = [(1, 0.50, 0.50), (999, 0.20, 0.80)]

        blended = position_aware_blend(fused, reranker_scores, tiers=custom_tiers)

        # c0 (rank 1): 0.50 * 1.0 + 0.50 * 0.5 = 0.75
        c0 = next(r for r in blended if r["chunk_id"] == "c0")
        assert pytest.approx(c0["score"], abs=1e-9) == 0.50 * 1.0 + 0.50 * 0.5

        # c1 (rank 2): 0.20 * 0.8 + 0.80 * 0.9 = 0.88
        c1 = next(r for r in blended if r["chunk_id"] == "c1")
        assert pytest.approx(c1["score"], abs=1e-9) == 0.20 * 0.8 + 0.80 * 0.9

        # Results are re-sorted by blended score, so c1 (0.88) should be first
        assert blended[0]["chunk_id"] == "c1"
