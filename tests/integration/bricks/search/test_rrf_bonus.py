"""Tests for RRF top-rank bonus (Issue #3773).

Verifies that documents ranked #1 in any input list get a +0.05 bonus
and those ranked #2-3 get +0.02, preventing dilution of perfect matches
across multi-source / query-expanded fusion.
"""

from nexus.bricks.search.fusion import (
    RRF_TOP1_BONUS,
    RRF_TOP3_BONUS,
    FusionConfig,
    FusionMethod,
    fuse_results,
    rrf_fusion,
    rrf_multi_fusion,
    rrf_weighted_fusion,
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


class TestRrfWeightedBonus:
    def test_weighted_top1_gets_bonus(self) -> None:
        """rrf_weighted_fusion also applies the top-rank bonus."""
        kw = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_weighted_fusion(kw, vec, alpha=0.5, k=60, limit=10, id_key=None)
        # alpha=0.5, rank=1 in both: 0.5*(1/61) + 0.5*(1/61) + RRF_TOP1_BONUS
        expected = 0.5 * (1.0 / 61) + 0.5 * (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_weighted_bonus_disabled(self) -> None:
        kw = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_weighted_fusion(
            kw, vec, alpha=0.5, k=60, limit=10, id_key=None, top_rank_bonus=False
        )
        expected = 0.5 * (1.0 / 61) + 0.5 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_alpha_zero_vector_rank1_gets_no_bonus(self) -> None:
        """alpha=0 means vector has zero weight; a vector rank-1 doc absent
        from keyword results must NOT receive the top-rank bonus (Issue #3773
        review)."""
        kw = [{"path": "kw_only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "vec_only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_weighted_fusion(kw, vec, alpha=0.0, k=60, limit=10, id_key=None)
        by_path = {r["path"]: r["score"] for r in results}
        # kw_only gets full keyword weight + bonus.
        assert abs(by_path["kw_only.txt"] - (1.0 * (1.0 / 61) + RRF_TOP1_BONUS)) < 1e-9
        # vec_only has zero weight and must NOT receive the bonus.
        assert by_path["vec_only.txt"] == 0.0

    def test_alpha_one_keyword_rank1_gets_no_bonus(self) -> None:
        """alpha=1 means keyword has zero weight; symmetric to the above."""
        kw = [{"path": "kw_only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "vec_only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_weighted_fusion(kw, vec, alpha=1.0, k=60, limit=10, id_key=None)
        by_path = {r["path"]: r["score"] for r in results}
        assert abs(by_path["vec_only.txt"] - (1.0 * (1.0 / 61) + RRF_TOP1_BONUS)) < 1e-9
        assert by_path["kw_only.txt"] == 0.0


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


class TestFuseResultsConfigFlag:
    def test_fuse_results_passes_top_rank_bonus_false(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        config = FusionConfig(method=FusionMethod.RRF, top_rank_bonus=False)
        results = fuse_results(kw, vec, config=config, limit=10, id_key=None)
        expected = 2 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_fuse_results_default_applies_bonus(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        results = fuse_results(kw, vec, config=None, limit=10, id_key=None)
        expected = 2 * (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_fuse_results_weighted_respects_flag(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        config = FusionConfig(method=FusionMethod.RRF_WEIGHTED, top_rank_bonus=False)
        results = fuse_results(kw, vec, config=config, limit=10, id_key=None)
        expected = 0.5 * (1.0 / 61) + 0.5 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9
