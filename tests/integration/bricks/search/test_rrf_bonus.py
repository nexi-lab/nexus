"""Tests for RRF top-rank bonus (Issue #3773).

Verifies that documents ranked #1 in any input list get a +0.05 bonus
and those ranked #2-3 get +0.02, preventing dilution of perfect matches
across multi-source / query-expanded fusion.
"""

from nexus.bricks.search.fusion import (
    RRF_TOP1_BONUS,
    rrf_multi_fusion,
)


class TestRrfMultiBonus:
    def test_multi_top1_gets_bonus_from_any_source(self) -> None:
        """rrf_multi_fusion: best rank across all sources drives bonus."""
        lists = [
            (
                "keyword",
                [
                    {"path": "perfect.txt", "chunk_index": 0, "score": 1.0},
                    {"path": "other.txt", "chunk_index": 0, "score": 1.0},
                ],
            ),
            (
                "vector",
                [
                    {"path": "unrelated.txt", "chunk_index": 0, "score": 1.0},
                    {"path": "other.txt", "chunk_index": 0, "score": 1.0},
                ],
            ),
            (
                "splade",
                [{"path": "perfect.txt", "chunk_index": 0, "score": 1.0}],
            ),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        # "perfect.txt" is rank 1 in keyword + splade -> gets TOP1 bonus.
        perfect = next(r for r in results if r["path"] == "perfect.txt")
        expected = (1.0 / 61) + (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(perfect["score"] - expected) < 1e-9

    def test_multi_bonus_disabled(self) -> None:
        lists = [
            ("keyword", [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]),
            ("vector", [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None, top_rank_bonus=False)
        expected = 2 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9
