"""Tests for RRF top-rank bonus (Issue #3773).

Verifies that documents ranked #1 in any input list get a +0.05 bonus
and those ranked #2-3 get +0.02, preventing dilution of perfect matches
across multi-source / query-expanded fusion.
"""

from nexus.bricks.search.fusion import (
    RRF_TOP1_BONUS,
    RRF_TOP3_BONUS,
    rrf_fusion,
)


class TestRrfTop1Bonus:
    def test_top1_keyword_only_beats_mediocre_both(self) -> None:
        """Issue #3773 scenario: #1 in keyword but absent from vector
        beats #3 in both without the bonus."""
        kw = [
            {"path": "perfect.txt", "chunk_index": 0, "score": 10.0},  # rank 1
            {"path": "x.txt", "chunk_index": 0, "score": 1.0},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},  # rank 3
        ]
        vec = [
            {"path": "y.txt", "chunk_index": 0, "score": 0.9},
            {"path": "z.txt", "chunk_index": 0, "score": 0.8},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},  # rank 3
        ]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        ranked_paths = [r["path"] for r in results]
        assert ranked_paths.index("perfect.txt") < ranked_paths.index("mediocre.txt")

    def test_bonus_disabled_preserves_legacy_behavior(self) -> None:
        kw = [
            {"path": "perfect.txt", "chunk_index": 0, "score": 10.0},
            {"path": "x.txt", "chunk_index": 0, "score": 1.0},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},
        ]
        vec = [
            {"path": "y.txt", "chunk_index": 0, "score": 0.9},
            {"path": "z.txt", "chunk_index": 0, "score": 0.8},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},
        ]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None, top_rank_bonus=False)
        ranked_paths = [r["path"] for r in results]
        # Without bonus, mediocre (in both) outranks perfect (single list).
        assert ranked_paths.index("mediocre.txt") < ranked_paths.index("perfect.txt")

    def test_rank1_receives_top1_bonus(self) -> None:
        """Single-doc fusion: score == 2 * 1/(k+1) + RRF_TOP1_BONUS."""
        kw = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        assert len(results) == 1
        expected = (1.0 / 61) + (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_rank3_receives_top3_bonus(self) -> None:
        """Doc at rank 3 in keyword only: 1/(k+3) + RRF_TOP3_BONUS."""
        kw = [
            {"path": "a.txt", "chunk_index": 0, "score": 1.0},
            {"path": "b.txt", "chunk_index": 0, "score": 1.0},
            {"path": "c.txt", "chunk_index": 0, "score": 1.0},
        ]
        vec: list[dict] = []
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        c_result = next(r for r in results if r["path"] == "c.txt")
        expected = (1.0 / 63) + RRF_TOP3_BONUS
        assert abs(c_result["score"] - expected) < 1e-9

    def test_rank4_receives_no_bonus(self) -> None:
        kw = [{"path": f"r{i}.txt", "chunk_index": 0, "score": 1.0} for i in range(5)]
        results = rrf_fusion(kw, [], k=60, limit=10, id_key=None)
        r4 = next(r for r in results if r["path"] == "r3.txt")  # zero-indexed -> rank 4
        expected = 1.0 / 64
        assert abs(r4["score"] - expected) < 1e-9
