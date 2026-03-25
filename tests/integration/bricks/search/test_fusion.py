"""Tests for fusion algorithms (Issue #3147 decision 9A).

Tests rrf_fusion, rrf_multi_fusion, weighted_fusion, and rrf_weighted_fusion.
Written BEFORE building the federated search dispatcher to ensure the
foundation is solid.
"""

from nexus.bricks.search.fusion import (
    normalize_scores_minmax,
    rrf_fusion,
    rrf_multi_fusion,
    rrf_weighted_fusion,
    weighted_fusion,
)
from nexus.bricks.search.results import BaseSearchResult

# =============================================================================
# normalize_scores_minmax
# =============================================================================


class TestNormalizeScoresMinmax:
    def test_empty_list(self) -> None:
        assert normalize_scores_minmax([]) == []

    def test_single_value(self) -> None:
        assert normalize_scores_minmax([5.0]) == [1.0]

    def test_identical_values(self) -> None:
        assert normalize_scores_minmax([3.0, 3.0, 3.0]) == [1.0, 1.0, 1.0]

    def test_ascending(self) -> None:
        result = normalize_scores_minmax([0.0, 5.0, 10.0])
        assert result == [0.0, 0.5, 1.0]

    def test_descending(self) -> None:
        result = normalize_scores_minmax([10.0, 5.0, 0.0])
        assert result == [1.0, 0.5, 0.0]


# =============================================================================
# rrf_fusion (2-way)
# =============================================================================


class TestRrfFusion:
    def test_basic_two_way(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 10.0}]
        vec = [{"path": "b.txt", "chunk_index": 0, "score": 0.95}]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        assert len(results) == 2
        # Both should have RRF scores
        for r in results:
            assert r["score"] > 0

    def test_overlapping_results_merged(self) -> None:
        """Same doc in both lists should be merged and ranked higher."""
        kw = [
            {"path": "shared.txt", "chunk_index": 0, "score": 5.0},
            {"path": "kw_only.txt", "chunk_index": 0, "score": 3.0},
        ]
        vec = [
            {"path": "shared.txt", "chunk_index": 0, "score": 0.9},
            {"path": "vec_only.txt", "chunk_index": 0, "score": 0.8},
        ]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        # shared.txt appears in both lists, so it should have highest RRF score
        assert results[0]["path"] == "shared.txt"
        assert len(results) == 3  # 3 unique docs

    def test_respects_limit(self) -> None:
        kw = [{"path": f"kw_{i}.txt", "chunk_index": 0, "score": 10 - i} for i in range(5)]
        vec = [{"path": f"vec_{i}.txt", "chunk_index": 0, "score": 0.9 - i * 0.1} for i in range(5)]
        results = rrf_fusion(kw, vec, k=60, limit=3, id_key=None)
        assert len(results) == 3

    def test_empty_keyword_list(self) -> None:
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 0.9}]
        results = rrf_fusion([], vec, k=60, limit=10, id_key=None)
        assert len(results) == 1

    def test_both_empty(self) -> None:
        results = rrf_fusion([], [], k=60, limit=10, id_key=None)
        assert results == []

    def test_accepts_dataclass_results(self) -> None:
        """rrf_fusion should handle BaseSearchResult dataclasses via _to_dict."""
        kw = [BaseSearchResult(path="a.txt", chunk_text="hello", score=5.0)]
        vec = [BaseSearchResult(path="b.txt", chunk_text="world", score=0.9)]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        assert len(results) == 2

    def test_custom_id_key(self) -> None:
        kw = [{"chunk_id": "c1", "path": "a.txt", "score": 5.0}]
        vec = [{"chunk_id": "c1", "path": "a.txt", "score": 0.9}]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key="chunk_id")
        # Same chunk_id should be merged
        assert len(results) == 1


# =============================================================================
# rrf_multi_fusion (N-way) — critical for federated search
# =============================================================================


class TestRrfMultiFusion:
    def test_basic_three_way(self) -> None:
        lists = [
            ("zone_a", [{"path": "a.txt", "chunk_index": 0, "score": 5.0}]),
            ("zone_b", [{"path": "b.txt", "chunk_index": 0, "score": 3.0}]),
            ("zone_c", [{"path": "c.txt", "chunk_index": 0, "score": 1.0}]),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        assert len(results) == 3
        # All results should have positive RRF scores
        for r in results:
            assert r["score"] > 0

    def test_cross_source_dedup(self) -> None:
        """Same path:chunk_index across sources should be merged."""
        lists = [
            ("zone_a", [{"path": "shared.txt", "chunk_index": 0, "score": 5.0}]),
            ("zone_b", [{"path": "shared.txt", "chunk_index": 0, "score": 3.0}]),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        # shared.txt:0 appears in both — should be merged into one result
        assert len(results) == 1
        # Score should be higher than single-source
        single_rrf = 1.0 / (60 + 1)
        assert results[0]["score"] > single_rrf

    def test_custom_id_key_for_zone_dedup(self) -> None:
        """Using zone_qualified_path as id_key prevents cross-zone dedup."""
        lists = [
            (
                "zone_a",
                [
                    {
                        "path": "doc.txt",
                        "chunk_index": 0,
                        "score": 5.0,
                        "zone_qualified_path": "zone_a:doc.txt",
                    },
                ],
            ),
            (
                "zone_b",
                [
                    {
                        "path": "doc.txt",
                        "chunk_index": 0,
                        "score": 3.0,
                        "zone_qualified_path": "zone_b:doc.txt",
                    },
                ],
            ),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key="zone_qualified_path")
        # Different zone_qualified_path = different results, not merged
        assert len(results) == 2

    def test_single_source_passthrough(self) -> None:
        lists = [
            (
                "zone_a",
                [
                    {"path": "a.txt", "chunk_index": 0, "score": 5.0},
                    {"path": "b.txt", "chunk_index": 0, "score": 3.0},
                ],
            ),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        assert len(results) == 2
        # Rank order should be preserved
        assert results[0]["path"] == "a.txt"

    def test_empty_source_ignored(self) -> None:
        lists = [
            ("zone_a", [{"path": "a.txt", "chunk_index": 0, "score": 5.0}]),
            ("zone_b", []),  # empty
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        assert len(results) == 1

    def test_all_empty(self) -> None:
        lists = [("zone_a", []), ("zone_b", [])]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        assert results == []

    def test_respects_limit(self) -> None:
        lists = [
            (
                "zone_a",
                [{"path": f"a_{i}.txt", "chunk_index": 0, "score": 10 - i} for i in range(10)],
            ),
            (
                "zone_b",
                [{"path": f"b_{i}.txt", "chunk_index": 0, "score": 10 - i} for i in range(10)],
            ),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=5, id_key=None)
        assert len(results) == 5

    def test_source_scores_tagged(self) -> None:
        """Each source should get a '{source_name}_score' field."""
        lists = [
            ("keyword", [{"path": "a.txt", "chunk_index": 0, "score": 5.0}]),
            ("vector", [{"path": "a.txt", "chunk_index": 0, "score": 0.9}]),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        assert len(results) == 1
        assert "keyword_score" in results[0]
        assert "vector_score" in results[0]

    def test_accepts_dataclass_results(self) -> None:
        lists = [
            ("zone_a", [BaseSearchResult(path="a.txt", chunk_text="hello", score=5.0)]),
            ("zone_b", [BaseSearchResult(path="b.txt", chunk_text="world", score=3.0)]),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        assert len(results) == 2

    def test_many_sources(self) -> None:
        """Simulate 10-zone federated search."""
        lists = [
            (f"zone_{i}", [{"path": f"doc_{i}.txt", "chunk_index": 0, "score": float(10 - i)}])
            for i in range(10)
        ]
        results = rrf_multi_fusion(lists, k=60, limit=5, id_key=None)
        assert len(results) == 5


# =============================================================================
# rrf_weighted_fusion
# =============================================================================


class TestRrfWeightedFusion:
    def test_alpha_zero_favors_keyword(self) -> None:
        kw = [{"path": "kw.txt", "chunk_index": 0, "score": 10.0}]
        vec = [{"path": "vec.txt", "chunk_index": 0, "score": 0.9}]
        results = rrf_weighted_fusion(kw, vec, alpha=0.0, k=60, limit=10, id_key=None)
        # alpha=0 means keyword gets full weight
        assert results[0]["path"] == "kw.txt"

    def test_alpha_one_favors_vector(self) -> None:
        kw = [{"path": "kw.txt", "chunk_index": 0, "score": 10.0}]
        vec = [{"path": "vec.txt", "chunk_index": 0, "score": 0.9}]
        results = rrf_weighted_fusion(kw, vec, alpha=1.0, k=60, limit=10, id_key=None)
        assert results[0]["path"] == "vec.txt"


# =============================================================================
# weighted_fusion
# =============================================================================


class TestWeightedFusion:
    def test_basic(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 5.0}]
        vec = [{"path": "b.txt", "chunk_index": 0, "score": 0.9}]
        results = weighted_fusion(kw, vec, alpha=0.5, limit=10, id_key=None)
        assert len(results) == 2

    def test_both_empty(self) -> None:
        results = weighted_fusion([], [], alpha=0.5, limit=10, id_key=None)
        assert results == []
