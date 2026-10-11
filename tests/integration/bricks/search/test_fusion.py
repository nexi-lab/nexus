"""Cross-zone reciprocal rank fusion preserves ranks and result metadata."""

from nexus.bricks.search.fusion import rrf_multi_fusion
from nexus.bricks.search.results import BaseSearchResult


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
