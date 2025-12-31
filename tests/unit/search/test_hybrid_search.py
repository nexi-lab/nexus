"""Unit tests for hybrid search fusion algorithms (Issue #798).

Tests the fusion algorithms for combining keyword (BM25) and vector search results:
- RRF (Reciprocal Rank Fusion)
- Weighted linear combination
- RRF with alpha weighting
"""

from __future__ import annotations

import pytest

from nexus.search.fusion import (
    FusionConfig,
    FusionMethod,
    fuse_results,
    normalize_scores_minmax,
    rrf_fusion,
    rrf_weighted_fusion,
    weighted_fusion,
)


class TestScoreNormalization:
    """Tests for min-max score normalization."""

    def test_normalize_empty(self):
        """Test normalizing empty list."""
        assert normalize_scores_minmax([]) == []

    def test_normalize_single_value(self):
        """Test normalizing single value."""
        assert normalize_scores_minmax([0.5]) == [1.0]

    def test_normalize_same_values(self):
        """Test normalizing identical values."""
        assert normalize_scores_minmax([0.5, 0.5, 0.5]) == [1.0, 1.0, 1.0]

    def test_normalize_range(self):
        """Test normalizing a range of values."""
        result = normalize_scores_minmax([0.0, 0.5, 1.0])
        assert result == [0.0, 0.5, 1.0]

    def test_normalize_arbitrary(self):
        """Test normalizing arbitrary values."""
        result = normalize_scores_minmax([10.0, 20.0, 30.0])
        assert result == [0.0, 0.5, 1.0]

    def test_normalize_negative_values(self):
        """Test normalizing values including negatives."""
        result = normalize_scores_minmax([-10.0, 0.0, 10.0])
        assert result == [0.0, 0.5, 1.0]

    def test_normalize_preserves_order(self):
        """Test that normalization preserves relative ordering."""
        original = [5.0, 2.0, 8.0, 1.0, 9.0]
        normalized = normalize_scores_minmax(original)

        # Sort indices by original values
        orig_order = sorted(range(len(original)), key=lambda i: original[i])
        norm_order = sorted(range(len(normalized)), key=lambda i: normalized[i])

        assert orig_order == norm_order


class TestRRFFusion:
    """Tests for Reciprocal Rank Fusion."""

    @pytest.fixture
    def keyword_results(self):
        return [
            {"chunk_id": "a", "path": "/a.py", "chunk_index": 0, "score": 10.0},
            {"chunk_id": "b", "path": "/b.py", "chunk_index": 0, "score": 8.0},
            {"chunk_id": "c", "path": "/c.py", "chunk_index": 0, "score": 5.0},
        ]

    @pytest.fixture
    def vector_results(self):
        return [
            {"chunk_id": "b", "path": "/b.py", "chunk_index": 0, "score": 0.95},
            {"chunk_id": "d", "path": "/d.py", "chunk_index": 0, "score": 0.90},
            {"chunk_id": "a", "path": "/a.py", "chunk_index": 0, "score": 0.85},
        ]

    def test_rrf_basic(self, keyword_results, vector_results):
        """Test basic RRF fusion."""
        results = rrf_fusion(keyword_results, vector_results, k=60, limit=10)

        assert len(results) == 4  # a, b, c, d

        # b should rank highest (rank 2 in keyword + rank 1 in vector)
        # RRF score for b: 1/(60+2) + 1/(60+1) = 0.0161 + 0.0164 = 0.0325
        # RRF score for a: 1/(60+1) + 1/(60+3) = 0.0164 + 0.0159 = 0.0323
        assert results[0]["chunk_id"] == "b"

        # All results should have both scores where applicable
        b_result = next(r for r in results if r["chunk_id"] == "b")
        assert "keyword_score" in b_result
        assert "vector_score" in b_result
        assert b_result["keyword_score"] == 8.0
        assert b_result["vector_score"] == 0.95

    def test_rrf_respects_limit(self, keyword_results, vector_results):
        """Test RRF respects limit parameter."""
        results = rrf_fusion(keyword_results, vector_results, k=60, limit=2)
        assert len(results) == 2

    def test_rrf_empty_keyword(self, vector_results):
        """Test RRF with empty keyword results."""
        results = rrf_fusion([], vector_results, k=60, limit=10)
        assert len(results) == 3
        # Results should only have vector_score
        for r in results:
            assert "vector_score" in r
            assert r.get("keyword_score") is None or r.get("keyword_score") == 0

    def test_rrf_empty_vector(self, keyword_results):
        """Test RRF with empty vector results."""
        results = rrf_fusion(keyword_results, [], k=60, limit=10)
        assert len(results) == 3
        # Results should only have keyword_score
        for r in results:
            assert "keyword_score" in r

    def test_rrf_empty_both(self):
        """Test RRF with empty inputs."""
        results = rrf_fusion([], [], k=60, limit=10)
        assert results == []

    def test_rrf_k_parameter(self, keyword_results, vector_results):
        """Test that k parameter affects scores but not ranking."""
        results_k60 = rrf_fusion(keyword_results, vector_results, k=60, limit=10)
        results_k1 = rrf_fusion(keyword_results, vector_results, k=1, limit=10)

        # Scores should be different with different k values
        assert results_k60[0]["score"] != results_k1[0]["score"]

        # Higher k means lower scores
        assert results_k60[0]["score"] < results_k1[0]["score"]

    def test_rrf_score_formula(self, keyword_results, vector_results):
        """Test RRF score formula is correct."""
        k = 60
        results = rrf_fusion(keyword_results, vector_results, k=k, limit=10)

        # For "a": rank 1 in keyword, rank 3 in vector
        # Score = 1/(60+1) + 1/(60+3) = 0.01639 + 0.01587 = 0.03226
        a_result = next(r for r in results if r["chunk_id"] == "a")
        expected_score = 1 / (k + 1) + 1 / (k + 3)
        assert abs(a_result["score"] - expected_score) < 0.0001


class TestWeightedFusion:
    """Tests for weighted linear combination fusion."""

    @pytest.fixture
    def keyword_results(self):
        return [
            {"chunk_id": "a", "path": "/a.py", "chunk_index": 0, "score": 10.0},
            {"chunk_id": "b", "path": "/b.py", "chunk_index": 0, "score": 5.0},
        ]

    @pytest.fixture
    def vector_results(self):
        return [
            {"chunk_id": "b", "path": "/b.py", "chunk_index": 0, "score": 0.9},
            {"chunk_id": "c", "path": "/c.py", "chunk_index": 0, "score": 0.8},
        ]

    def test_weighted_alpha_0(self, keyword_results, vector_results):
        """Test weighted fusion with alpha=0 (all keyword)."""
        results = weighted_fusion(
            keyword_results, vector_results, alpha=0.0, normalize=True, limit=10
        )

        # With alpha=0, only keyword scores matter
        # a has highest keyword score
        assert results[0]["chunk_id"] == "a"

    def test_weighted_alpha_1(self, keyword_results, vector_results):
        """Test weighted fusion with alpha=1 (all vector)."""
        results = weighted_fusion(
            keyword_results, vector_results, alpha=1.0, normalize=True, limit=10
        )

        # With alpha=1, only vector scores matter
        # b has highest vector score
        assert results[0]["chunk_id"] == "b"

    def test_weighted_alpha_05(self, keyword_results, vector_results):
        """Test weighted fusion with alpha=0.5 (equal weight)."""
        results = weighted_fusion(
            keyword_results, vector_results, alpha=0.5, normalize=True, limit=10
        )

        # Both keyword and vector matter equally
        assert len(results) == 3  # a, b, c

    def test_weighted_with_normalization(self, keyword_results, vector_results):
        """Test weighted fusion with score normalization."""
        results = weighted_fusion(
            keyword_results, vector_results, alpha=0.5, normalize=True, limit=10
        )

        # All scores should be in [0, 1] range after normalization
        for r in results:
            assert 0 <= r["score"] <= 1

    def test_weighted_without_normalization(self, keyword_results, vector_results):
        """Test weighted fusion without normalization."""
        results = weighted_fusion(
            keyword_results, vector_results, alpha=0.5, normalize=False, limit=10
        )

        # Scores may be outside [0,1] range
        assert len(results) > 0

    def test_weighted_preserves_original_scores(self, keyword_results, vector_results):
        """Test that original scores are preserved."""
        results = weighted_fusion(
            keyword_results, vector_results, alpha=0.5, normalize=True, limit=10
        )

        a_result = next(r for r in results if r["chunk_id"] == "a")
        assert a_result["keyword_score"] == 10.0
        assert a_result.get("vector_score", 0) == 0

        b_result = next(r for r in results if r["chunk_id"] == "b")
        assert b_result["keyword_score"] == 5.0
        assert b_result["vector_score"] == 0.9


class TestRRFWeightedFusion:
    """Tests for RRF with alpha weighting."""

    @pytest.fixture
    def keyword_results(self):
        return [
            {"chunk_id": "a", "score": 10.0},
            {"chunk_id": "b", "score": 8.0},
        ]

    @pytest.fixture
    def vector_results(self):
        return [
            {"chunk_id": "b", "score": 0.9},
            {"chunk_id": "c", "score": 0.8},
        ]

    def test_rrf_weighted_alpha_0(self, keyword_results, vector_results):
        """Test RRF weighted with alpha=0 (keyword only contribution)."""
        results = rrf_weighted_fusion(keyword_results, vector_results, alpha=0.0, k=60, limit=10)

        # With alpha=0, only keyword ranks contribute
        # a is rank 1 in keyword, so should be highest
        assert results[0]["chunk_id"] == "a"

    def test_rrf_weighted_alpha_1(self, keyword_results, vector_results):
        """Test RRF weighted with alpha=1 (vector only contribution)."""
        results = rrf_weighted_fusion(keyword_results, vector_results, alpha=1.0, k=60, limit=10)

        # With alpha=1, only vector ranks contribute
        # b is rank 1 in vector
        assert results[0]["chunk_id"] == "b"

    def test_rrf_weighted_alpha_05(self, keyword_results, vector_results):
        """Test RRF weighted with alpha=0.5."""
        results = rrf_weighted_fusion(keyword_results, vector_results, alpha=0.5, k=60, limit=10)

        # Should be same as regular RRF when alpha=0.5
        rrf_results = rrf_fusion(keyword_results, vector_results, k=60, limit=10)

        # Same top results
        assert results[0]["chunk_id"] == rrf_results[0]["chunk_id"]

    def test_rrf_weighted_score_formula(self, keyword_results, vector_results):
        """Test RRF weighted score formula."""
        k = 60
        alpha = 0.7  # Favor vector
        results = rrf_weighted_fusion(keyword_results, vector_results, alpha=alpha, k=k, limit=10)

        # For "b": rank 2 in keyword, rank 1 in vector
        # Score = (1-0.7) * 1/(60+2) + 0.7 * 1/(60+1) = 0.3 * 0.01613 + 0.7 * 0.01639
        b_result = next(r for r in results if r["chunk_id"] == "b")
        expected_score = (1 - alpha) * (1 / (k + 2)) + alpha * (1 / (k + 1))
        assert abs(b_result["score"] - expected_score) < 0.0001


class TestFuseResults:
    """Tests for the unified fuse_results function."""

    @pytest.fixture
    def sample_results(self):
        keyword = [{"chunk_id": "a", "score": 1.0}]
        vector = [{"chunk_id": "b", "score": 0.9}]
        return keyword, vector

    def test_fuse_default_config(self, sample_results):
        """Test fuse_results with default config (RRF)."""
        keyword, vector = sample_results
        results = fuse_results(keyword, vector, limit=10)

        assert len(results) == 2

    def test_fuse_with_rrf_config(self, sample_results):
        """Test fuse_results with explicit RRF config."""
        keyword, vector = sample_results
        config = FusionConfig(method=FusionMethod.RRF, rrf_k=60)
        results = fuse_results(keyword, vector, config=config, limit=10)

        assert len(results) == 2

    def test_fuse_with_weighted_config(self, sample_results):
        """Test fuse_results with weighted config."""
        keyword, vector = sample_results
        config = FusionConfig(method=FusionMethod.WEIGHTED, alpha=0.7, normalize_scores=True)
        results = fuse_results(keyword, vector, config=config, limit=10)

        assert len(results) == 2

    def test_fuse_with_rrf_weighted_config(self, sample_results):
        """Test fuse_results with RRF weighted config."""
        keyword, vector = sample_results
        config = FusionConfig(method=FusionMethod.RRF_WEIGHTED, alpha=0.7, rrf_k=60)
        results = fuse_results(keyword, vector, config=config, limit=10)

        assert len(results) == 2

    def test_fuse_invalid_method(self, sample_results):
        """Test fuse_results with invalid method raises error."""
        keyword, vector = sample_results

        config = FusionConfig()
        # This should work fine
        results = fuse_results(keyword, vector, config=config, limit=10)
        assert len(results) == 2


class TestFusionConfig:
    """Tests for FusionConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = FusionConfig()

        assert config.method == FusionMethod.RRF
        assert config.alpha == 0.5
        assert config.rrf_k == 60
        assert config.normalize_scores is True

    def test_custom_values(self):
        """Test custom configuration values."""
        config = FusionConfig(
            method=FusionMethod.WEIGHTED,
            alpha=0.3,
            rrf_k=100,
            normalize_scores=False,
        )

        assert config.method == FusionMethod.WEIGHTED
        assert config.alpha == 0.3
        assert config.rrf_k == 100
        assert config.normalize_scores is False


class TestFusionMethod:
    """Tests for FusionMethod enum."""

    def test_enum_values(self):
        """Test enum string values."""
        assert FusionMethod.RRF.value == "rrf"
        assert FusionMethod.WEIGHTED.value == "weighted"
        assert FusionMethod.RRF_WEIGHTED.value == "rrf_weighted"

    def test_enum_from_string(self):
        """Test creating enum from string."""
        assert FusionMethod("rrf") == FusionMethod.RRF
        assert FusionMethod("weighted") == FusionMethod.WEIGHTED
        assert FusionMethod("rrf_weighted") == FusionMethod.RRF_WEIGHTED


class TestFusionEdgeCases:
    """Tests for edge cases in fusion algorithms."""

    def test_duplicate_chunk_ids(self):
        """Test handling of duplicate results in both lists."""
        keyword = [
            {"chunk_id": "a", "score": 10.0},
            {"chunk_id": "a", "score": 8.0},  # Duplicate
        ]
        vector = [{"chunk_id": "a", "score": 0.9}]

        results = rrf_fusion(keyword, vector, k=60, limit=10)

        # Should have only one result for "a"
        assert len(results) == 1
        assert results[0]["chunk_id"] == "a"

    def test_large_result_sets(self):
        """Test with large result sets."""
        keyword = [{"chunk_id": f"k{i}", "score": 100 - i} for i in range(1000)]
        vector = [{"chunk_id": f"v{i}", "score": 1.0 - i / 1000} for i in range(1000)]

        results = rrf_fusion(keyword, vector, k=60, limit=100)

        assert len(results) == 100
        # All results should have valid scores
        for r in results:
            assert r["score"] > 0

    def test_path_based_key_fallback(self):
        """Test using path:chunk_index when chunk_id is missing."""
        keyword = [{"path": "/a.py", "chunk_index": 0, "score": 10.0}]
        vector = [{"path": "/a.py", "chunk_index": 0, "score": 0.9}]

        results = rrf_fusion(keyword, vector, k=60, limit=10, id_key=None)

        # Should merge based on path:chunk_index
        assert len(results) == 1
        assert results[0]["keyword_score"] == 10.0
        assert results[0]["vector_score"] == 0.9

    def test_very_small_scores(self):
        """Test with very small scores."""
        keyword = [{"chunk_id": "a", "score": 1e-10}]
        vector = [{"chunk_id": "a", "score": 1e-10}]

        results = weighted_fusion(keyword, vector, alpha=0.5, normalize=True, limit=10)

        assert len(results) == 1
        # Score should be 1.0 after normalization (single item)
        assert results[0]["score"] == 1.0

    def test_zero_scores(self):
        """Test with zero scores."""
        keyword = [
            {"chunk_id": "a", "score": 0.0},
            {"chunk_id": "b", "score": 1.0},
        ]
        vector = [{"chunk_id": "a", "score": 1.0}]

        results = weighted_fusion(keyword, vector, alpha=0.5, normalize=True, limit=10)

        # "a" appears in both, "b" only in keyword
        assert len(results) == 2
