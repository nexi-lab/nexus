"""Per-prefix ranking weight primitives (Issue #4544).

Covers the record-grain prefix lookup, the score-multiply helper with the
uplift-only floor-ratio gate, and idempotency (a result already stamped
with tier_boost is never boosted twice — batch_search re-attaches).
"""

from __future__ import annotations

from datetime import datetime

from nexus.bricks.search.daemon import _apply_tier_weight
from nexus.bricks.search.path_context import (
    PathContextRecord,
    lookup_in_records,
    lookup_record_in_records,
)
from nexus.bricks.search.results import BaseSearchResult


def _rec(prefix: str, weight: float | None = None) -> PathContextRecord:
    now = datetime(2026, 7, 31)
    return PathContextRecord(
        zone_id="root",
        path_prefix=prefix,
        description=f"desc:{prefix or 'root'}",
        created_at=now,
        updated_at=now,
        weight=weight,
    )


def _result(path: str = "chat/logs/a.md", score: float = 1.0) -> BaseSearchResult:
    return BaseSearchResult(path=path, chunk_text="", score=score)


class TestLookupRecord:
    def test_returns_longest_prefix_record(self) -> None:
        records = sorted(
            [_rec("chat", weight=0.9), _rec("chat/logs", weight=0.5)],
            key=lambda r: len(r.path_prefix),
            reverse=True,
        )
        rec = lookup_record_in_records(records, "chat/logs/a.md")
        assert rec is not None and rec.weight == 0.5

    def test_no_match_returns_none(self) -> None:
        assert lookup_record_in_records([_rec("docs")], "src/a.py") is None

    def test_description_wrapper_unchanged(self) -> None:
        # lookup_in_records keeps its historical contract.
        assert lookup_in_records([_rec("docs")], "docs/a.md") == "desc:docs"
        assert lookup_in_records([_rec("docs")], "src/a.py") is None


class TestApplyTierWeight:
    def test_none_and_one_are_noops(self) -> None:
        r = _result(score=1.0)
        assert _apply_tier_weight(r, None, top_score=1.0, floor_ratio=0.25) is False
        assert _apply_tier_weight(r, 1.0, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == 1.0 and r.tier_boost is None

    def test_demotion_applies_and_stamps(self) -> None:
        r = _result(score=1.0)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 0.5 and r.tier_boost == 0.5

    def test_demotion_ignores_floor_gate(self) -> None:
        # Far-below-top result still gets demoted.
        r = _result(score=0.01)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 0.005

    def test_uplift_blocked_below_floor(self) -> None:
        r = _result(score=0.1)  # 0.1 < 0.25 * 1.0
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == 0.1 and r.tier_boost is None

    def test_uplift_applies_above_floor(self) -> None:
        r = _result(score=0.5)
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 1.0 and r.tier_boost == 2.0

    def test_zero_ratio_disables_gate(self) -> None:
        r = _result(score=0.001)
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.0) is True

    def test_idempotent_on_stamped_result(self) -> None:
        # batch_search re-runs attach on already-boosted results: no w².
        r = _result(score=1.0)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == 0.5


class TestConfigKnobs:
    def test_defaults(self) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        cfg = DaemonConfig()
        assert cfg.tier_boost_overfetch_factor == 3
        assert cfg.tier_boost_floor_ratio == 0.25

    def test_env_overrides(self, monkeypatch) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        monkeypatch.setenv("NEXUS_SEARCH_TIER_BOOST_OVERFETCH", "5")
        monkeypatch.setenv("NEXUS_SEARCH_TIER_BOOST_FLOOR_RATIO", "0.5")
        cfg = DaemonConfig()
        assert cfg.tier_boost_overfetch_factor == 5
        assert cfg.tier_boost_floor_ratio == 0.5


class TestSlashNormalizedLookup:
    """Live-stack regression (#4544 e2e): daemon result paths carry a leading
    slash (``/workspace/...``) while the API's ``_normalize_prefix`` strips it
    from stored prefixes (``workspace/...``). The lookup must match anyway —
    without normalization, context and tier-weight attach silently never fire
    on real deployments."""

    def test_slashed_path_matches_normalized_prefix(self) -> None:
        records = [_rec("workspace/tierboost/noisy", weight=0.5)]
        rec = lookup_record_in_records(records, "/workspace/tierboost/noisy/log-a.md")
        assert rec is not None and rec.weight == 0.5

    def test_slashed_path_exact_prefix_match(self) -> None:
        records = [_rec("docs")]
        assert lookup_record_in_records(records, "/docs") is not None

    def test_unslashed_path_still_matches(self) -> None:
        records = [_rec("docs")]
        assert lookup_record_in_records(records, "docs/a.md") is not None

    def test_description_wrapper_matches_slashed_path(self) -> None:
        records = [_rec("docs")]
        assert lookup_in_records(records, "/docs/a.md") == "desc:docs"


class TestSignedScoreWeighting:
    """Codex review R1: pgvector semantic scores are raw cosine similarity
    and can be negative. A plain multiply would let a demotion RAISE a
    negative score (-0.2 × 0.5 = -0.1), inverting tier semantics; negative
    scores divide by the weight instead."""

    def test_demotion_worsens_negative_score(self) -> None:
        r = _result(score=-0.2)
        assert _apply_tier_weight(r, 0.5, top_score=-0.1, floor_ratio=0.25) is True
        assert r.score == -0.4  # more negative = worse rank

    def test_uplift_improves_negative_score(self) -> None:
        r = _result(score=-0.2)
        assert _apply_tier_weight(r, 2.0, top_score=-0.1, floor_ratio=0.25) is True
        assert r.score == -0.1  # toward zero = better rank

    def test_floor_gate_disabled_when_top_score_non_positive(self) -> None:
        # All-negative result sets: the ratio comparison is meaningless, so
        # uplifts must not be blanket-blocked.
        r = _result(score=-0.9)
        assert _apply_tier_weight(r, 2.0, top_score=-0.1, floor_ratio=0.25) is True

    def test_floor_gate_still_blocks_negative_score_under_positive_top(self) -> None:
        # A negative score is by definition below ratio*top when top > 0.
        r = _result(score=-0.2)
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == -0.2

    def test_positive_scores_unchanged_semantics(self) -> None:
        r = _result(score=0.8)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 0.4
