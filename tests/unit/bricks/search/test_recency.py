"""Pure recency-boost core (Issue #4543).

Covers the decay math, the intent detector, missing-mtime and
non-positive-score skips, and the byte-identity guarantee that a
zero-boost call performs no re-sort.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.search.recency import apply_recency_boost, has_recency_intent
from nexus.bricks.search.results import BaseSearchResult

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def _r(path: str, score: float) -> BaseSearchResult:
    return BaseSearchResult(path=path, chunk_text=path, score=score, chunk_index=0)


class TestDecayMath:
    def test_fresh_doc_gets_full_boost(self) -> None:
        """age=0 → score × (1 + w)."""
        r = _r("/fresh.md", 2.0)
        n = apply_recency_boost([r], {"/fresh.md": NOW}, weight=0.3, half_life_days=30.0, now=NOW)
        assert n == 1
        assert r.score == pytest.approx(2.0 * 1.3)
        assert r.recency_boost == pytest.approx(1.3)

    def test_half_life_old_doc_gets_half_boost(self) -> None:
        """age=H → score × (1 + w/2)."""
        r = _r("/mid.md", 2.0)
        mtimes = {"/mid.md": NOW - timedelta(days=30)}
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == pytest.approx(2.0 * 1.15)

    def test_ancient_doc_boost_approaches_one(self) -> None:
        r = _r("/old.md", 2.0)
        mtimes = {"/old.md": NOW - timedelta(days=36500)}
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert 2.0 < r.score < 2.002

    def test_future_mtime_clamped_to_max_boost(self) -> None:
        """Clock skew must never demote: age clamps to 0."""
        r = _r("/skew.md", 1.0)
        mtimes = {"/skew.md": NOW + timedelta(days=5)}
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == pytest.approx(1.3)

    def test_naive_mtime_treated_as_utc(self) -> None:
        """file_paths.updated_at is a naive UTC column."""
        r = _r("/naive.md", 1.0)
        mtimes = {"/naive.md": datetime(2026, 7, 31, 12, 0, 0)}  # naive == NOW in UTC
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == pytest.approx(1.3)


class TestSkips:
    def test_missing_mtime_left_untouched(self) -> None:
        r = _r("/unknown.md", 2.0)
        n = apply_recency_boost([r], {}, weight=0.3, half_life_days=30.0, now=NOW)
        assert n == 0
        assert r.score == 2.0
        assert r.recency_boost is None

    def test_non_positive_score_skipped(self) -> None:
        """Multiplicative boost on score<=0 would demote — skip defensively."""
        r = _r("/zero.md", 0.0)
        apply_recency_boost([r], {"/zero.md": NOW}, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == 0.0
        assert r.recency_boost is None

    def test_zero_weight_is_noop(self) -> None:
        r = _r("/a.md", 2.0)
        n = apply_recency_boost([r], {"/a.md": NOW}, weight=0.0, half_life_days=30.0, now=NOW)
        assert n == 0
        assert r.score == 2.0


class TestReordering:
    def test_newer_near_duplicate_overtakes_older(self) -> None:
        """Acceptance fixture: near-tie text scores, different mtimes —
        the newer doc must rank first once the boost fires."""
        older = _r("/old-dup.md", 1.00)
        newer = _r("/new-dup.md", 0.99)
        results = [older, newer]
        mtimes = {
            "/old-dup.md": NOW - timedelta(days=1095),
            "/new-dup.md": NOW - timedelta(days=1),
        }
        apply_recency_boost(results, mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert [r.path for r in results] == ["/new-dup.md", "/old-dup.md"]

    def test_no_boost_means_no_resort(self) -> None:
        """Byte-identity guard: when nothing fires, order is untouched even
        if the input was not score-sorted."""
        a, b = _r("/a.md", 0.5), _r("/b.md", 0.9)
        results = [a, b]  # deliberately unsorted
        apply_recency_boost(results, {}, weight=0.3, half_life_days=30.0, now=NOW)
        assert results == [a, b]

    def test_resort_preserves_list_subclass(self) -> None:
        """SearchResultList (list subclass carrying search_timing) must
        survive: boost sorts in place, never rebinds."""
        from nexus.bricks.search.daemon import SearchResult, SearchResultList

        rl = SearchResultList(
            [
                SearchResult(path="/old.md", chunk_text="x", score=1.0, chunk_index=0),
                SearchResult(path="/new.md", chunk_text="x", score=0.99, chunk_index=0),
            ],
            search_timing={"backend_ms": 1.0},
        )
        mtimes = {"/old.md": NOW - timedelta(days=365), "/new.md": NOW}
        apply_recency_boost(rl, mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert isinstance(rl, SearchResultList)
        assert rl.search_timing == {"backend_ms": 1.0}
        assert rl[0].path == "/new.md"


class TestIntent:
    @pytest.mark.parametrize(
        "query",
        ["latest deploy notes", "recent incidents", "what changed today", "Newest API docs"],
    )
    def test_recency_words_fire(self, query: str) -> None:
        assert has_recency_intent(query)

    @pytest.mark.parametrize(
        "query",
        [
            "authentication middleware",
            "history of the auth module",  # TEMPORAL word, not a recency word
            "before the migration",
        ],
    )
    def test_neutral_and_temporal_queries_do_not_fire(self, query: str) -> None:
        assert not has_recency_intent(query)


class TestConfig:
    def test_daemon_config_recency_defaults(self) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        cfg = DaemonConfig()
        assert cfg.recency_mode == "off"
        assert cfg.recency_weight == pytest.approx(0.3)
        assert cfg.recency_half_life_days == pytest.approx(30.0)

    def test_daemon_config_recency_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        monkeypatch.setenv("NEXUS_SEARCH_RECENCY", "AUTO")
        monkeypatch.setenv("NEXUS_SEARCH_RECENCY_WEIGHT", "0.5")
        monkeypatch.setenv("NEXUS_SEARCH_RECENCY_HALF_LIFE_DAYS", "7")
        cfg = DaemonConfig()
        assert cfg.recency_mode == "auto"  # normalized lowercase
        assert cfg.recency_weight == pytest.approx(0.5)
        assert cfg.recency_half_life_days == pytest.approx(7.0)

    def test_daemon_stats_has_recency_failure_counter(self) -> None:
        from nexus.bricks.search.daemon import DaemonStats

        assert DaemonStats().recency_attach_failures == 0

    def test_get_stats_exposes_recency_failure_counter(self) -> None:
        """Verify recency_attach_failures is wired into get_stats() output."""
        import inspect

        from nexus.bricks.search.daemon import SearchDaemon

        src = inspect.getsource(SearchDaemon.get_stats)
        assert "recency_attach_failures" in src
