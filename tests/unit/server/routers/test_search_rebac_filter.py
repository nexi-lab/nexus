"""Unit tests for the search ReBAC filter helper.

Covers `_apply_rebac_filter` and `_normalize_path` in
`nexus.server.api.v2.routers.search`. These helpers are the sole
enforcement point for file-level permissions on search responses
(Decision #17). They are on the hot path for every search, grep, and
glob query over HTTP, so we want dense branch coverage here.

Backfills the coverage gap flagged during the review of issue #3701.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.rebac_filter import (
    REBAC_OVERFETCH_FACTOR as _REBAC_OVERFETCH_FACTOR,
)
from nexus.lib.rebac_filter import (
    apply_rebac_filter as _apply_rebac_filter,
)
from nexus.lib.rebac_filter import (
    compute_rebac_fetch_limit as _compute_rebac_fetch_limit,
)
from nexus.lib.rebac_filter import (
    normalize_path as _normalize_path,
)
from nexus.lib.rebac_filter import (
    rebac_denial_stats as _rebac_denial_stats,
)
from nexus.server.api.v2.routers.search import (
    _serialize_search_result,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubResult:
    """Minimal stand-in for a search result (only needs `.path`)."""

    path: str
    marker: str = ""


def _make_enforcer(permitted: list[str] | None = None) -> MagicMock:
    """Create a mock PermissionEnforcer that permits the given paths."""
    enforcer = MagicMock()
    enforcer.filter_search_results = MagicMock(return_value=list(permitted or []))
    return enforcer


def _auth(**kwargs: Any) -> dict[str, Any]:
    """Build an auth_result dict with sensible defaults."""
    base: dict[str, Any] = {
        "authenticated": True,
        "subject_id": "user:alice",
        "user_id": "user:alice",
        "zone_id": "root",
        "is_admin": False,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _normalize_path
# ---------------------------------------------------------------------------


class TestNormalizePath:
    def test_absolute_path_unchanged(self) -> None:
        assert _normalize_path("/foo/bar.py") == "/foo/bar.py"

    def test_relative_path_gets_leading_slash(self) -> None:
        assert _normalize_path("foo/bar.py") == "/foo/bar.py"

    def test_root_path_unchanged(self) -> None:
        assert _normalize_path("/") == "/"

    def test_empty_string_gets_leading_slash(self) -> None:
        # Edge: shouldn't happen in practice, but shouldn't crash either.
        assert _normalize_path("") == "/"


# ---------------------------------------------------------------------------
# _apply_rebac_filter — no-op paths
# ---------------------------------------------------------------------------


class TestApplyRebacFilterNoOpPaths:
    def test_none_enforcer_returns_input_unchanged(self) -> None:
        results = [_StubResult("/a.py"), _StubResult("/b.py")]
        filtered, filter_ms = _apply_rebac_filter(
            results=results,
            permission_enforcer=None,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )
        assert filtered is results  # identity, not just equality
        assert filter_ms == 0.0

    def test_enforcer_without_filter_search_results_method(self) -> None:
        """Defensive branch: duck-type fallback."""
        bogus = object()  # no `filter_search_results` attribute
        results = [_StubResult("/a.py")]
        filtered, filter_ms = _apply_rebac_filter(
            results=results,
            permission_enforcer=bogus,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )
        assert filtered is results
        assert filter_ms == 0.0

    def test_empty_results_no_enforcer_call(self) -> None:
        """Empty-in should not waste a round-trip to the enforcer."""
        enforcer = _make_enforcer(permitted=[])
        filtered, filter_ms = _apply_rebac_filter(
            results=[],
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )
        assert filtered == []
        # filter_ms should still be populated (even if ~0) because we did
        # execute the filter call with an empty list.
        assert filter_ms >= 0.0
        enforcer.filter_search_results.assert_called_once_with(
            [],
            user_id="user:alice",
            zone_id=ROOT_ZONE_ID,
            is_admin=False,
        )


# ---------------------------------------------------------------------------
# _apply_rebac_filter — filtering behaviour
# ---------------------------------------------------------------------------


class TestApplyRebacFilterBehaviour:
    def test_all_paths_permitted_preserves_order(self) -> None:
        results = [
            _StubResult("/a.py", marker="first"),
            _StubResult("/b.py", marker="second"),
            _StubResult("/c.py", marker="third"),
        ]
        enforcer = _make_enforcer(permitted=["/a.py", "/b.py", "/c.py"])

        filtered, filter_ms = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )

        assert [r.marker for r in filtered] == ["first", "second", "third"]
        assert filter_ms >= 0.0

    def test_partial_denial_preserves_order_of_permitted(self) -> None:
        results = [
            _StubResult("/a.py", marker="first"),
            _StubResult("/secret.py", marker="denied"),
            _StubResult("/b.py", marker="third"),
        ]
        enforcer = _make_enforcer(permitted=["/a.py", "/b.py"])

        filtered, _ = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )

        assert [r.marker for r in filtered] == ["first", "third"]

    def test_all_denied_returns_empty_list(self) -> None:
        results = [_StubResult("/a.py"), _StubResult("/b.py")]
        enforcer = _make_enforcer(permitted=[])

        filtered, _ = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )

        assert filtered == []

    def test_relative_paths_are_normalised_before_enforcer_call(self) -> None:
        results = [_StubResult("a.py"), _StubResult("nested/b.py")]
        enforcer = _make_enforcer(permitted=["/a.py", "/nested/b.py"])

        filtered, _ = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )

        # Enforcer was invoked with absolute paths.
        call = enforcer.filter_search_results.call_args
        assert call.args[0] == ["/a.py", "/nested/b.py"]
        # Both results came back because the enforcer permitted both.
        assert len(filtered) == 2

    def test_duplicate_paths_all_preserved(self) -> None:
        """Multiple results sharing a path are ALL preserved (#3731).

        The previous path_map dict collapsed duplicates, silently
        dropping grep lines from the same file. The two-pass approach
        deduplicates paths for the permission check but keeps every
        original result row.
        """
        results = [
            _StubResult("/a.py", marker="first"),
            _StubResult("/a.py", marker="second"),
        ]
        enforcer = _make_enforcer(permitted=["/a.py"])

        filtered, _ = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )

        assert len(filtered) == 2
        assert filtered[0].marker == "first"
        assert filtered[1].marker == "second"
        # Enforcer should see only one unique path, not two.
        call = enforcer.filter_search_results.call_args
        assert call.args[0] == ["/a.py"]


# ---------------------------------------------------------------------------
# _apply_rebac_filter — auth_result extraction
# ---------------------------------------------------------------------------


class TestApplyRebacFilterAuthExtraction:
    def test_subject_id_takes_precedence_over_user_id(self) -> None:
        enforcer = _make_enforcer(permitted=["/a.py"])
        _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=_auth(subject_id="user:subject", user_id="user:legacy"),
            zone_id=ROOT_ZONE_ID,
        )
        call = enforcer.filter_search_results.call_args
        assert call.kwargs["user_id"] == "user:subject"

    def test_falls_back_to_user_id_when_subject_id_missing(self) -> None:
        enforcer = _make_enforcer(permitted=["/a.py"])
        auth = _auth()
        auth.pop("subject_id")  # no subject_id at all
        _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=auth,
            zone_id=ROOT_ZONE_ID,
        )
        assert enforcer.filter_search_results.call_args.kwargs["user_id"] == "user:alice"

    def test_anonymous_fallback_when_both_identities_missing(self) -> None:
        enforcer = _make_enforcer(permitted=["/a.py"])
        _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result={"authenticated": False},
            zone_id=ROOT_ZONE_ID,
        )
        assert enforcer.filter_search_results.call_args.kwargs["user_id"] == "anonymous"

    def test_is_admin_flag_propagates(self) -> None:
        enforcer = _make_enforcer(permitted=["/a.py"])
        _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=_auth(is_admin=True),
            zone_id=ROOT_ZONE_ID,
        )
        assert enforcer.filter_search_results.call_args.kwargs["is_admin"] is True

    def test_is_admin_truthy_values_coerced_to_bool(self) -> None:
        """The helper does `bool(...)` on is_admin; non-bool truthies should work."""
        enforcer = _make_enforcer(permitted=["/a.py"])
        _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=_auth(is_admin="yes"),  # non-bool truthy
            zone_id=ROOT_ZONE_ID,
        )
        assert enforcer.filter_search_results.call_args.kwargs["is_admin"] is True

    def test_zone_id_is_passed_through_to_enforcer(self) -> None:
        enforcer = _make_enforcer(permitted=["/a.py"])
        _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id="tenant-42",
        )
        assert enforcer.filter_search_results.call_args.kwargs["zone_id"] == "tenant-42"


# ---------------------------------------------------------------------------
# _apply_rebac_filter — filter_ms timing
# ---------------------------------------------------------------------------


class TestApplyRebacFilterTiming:
    def test_filter_ms_is_non_negative_when_enforcer_is_used(self) -> None:
        enforcer = _make_enforcer(permitted=["/a.py"])
        _, filter_ms = _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )
        assert filter_ms >= 0.0

    def test_filter_ms_measures_enforcer_work(self) -> None:
        """When the enforcer sleeps, the returned filter_ms reflects it."""
        import time

        def slow_filter(*_args: Any, **_kwargs: Any) -> list[str]:
            time.sleep(0.01)  # 10ms — well above timing noise
            return ["/a.py"]

        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(side_effect=slow_filter)

        _, filter_ms = _apply_rebac_filter(
            results=[_StubResult("/a.py")],
            permission_enforcer=enforcer,
            auth_result=_auth(),
            zone_id=ROOT_ZONE_ID,
        )

        # Sanity bound: should be > 5ms (well above noise) and < 5000ms (nothing insane).
        assert filter_ms > 5.0
        assert filter_ms < 5000.0


# ---------------------------------------------------------------------------
# _serialize_search_result (#3701 — Issue 5A)
# ---------------------------------------------------------------------------


@dataclass
class _SearchHit:
    """Stand-in for BaseSearchResult — only fields used by serialization."""

    path: str = "/src/a.py"
    chunk_text: str = "def foo():\n    pass"
    score: float = 0.12345678
    chunk_index: int = 2
    line_start: int | None = 10
    line_end: int | None = 12
    keyword_score: float | None = None
    vector_score: float | None = None
    splade_score: float | None = None
    reranker_score: float | None = None


class TestSerializeSearchResult:
    def test_core_fields_present(self) -> None:
        d = _serialize_search_result(_SearchHit())
        assert d["path"] == "/src/a.py"
        assert d["chunk_index"] == 2
        assert d["line_start"] == 10
        assert d["line_end"] == 12

    def test_score_is_rounded_to_4_decimals(self) -> None:
        d = _serialize_search_result(_SearchHit(score=0.12345678))
        assert d["score"] == 0.1235

    def test_keyword_and_vector_scores_rounded(self) -> None:
        d = _serialize_search_result(_SearchHit(keyword_score=0.88888, vector_score=0.77777))
        assert d["keyword_score"] == 0.8889
        assert d["vector_score"] == 0.7778

    def test_zero_scores_collapse_to_none(self) -> None:
        """Preserves legacy ``if score else None`` behaviour."""
        d = _serialize_search_result(_SearchHit(keyword_score=0.0, vector_score=0.0))
        assert d["keyword_score"] is None
        assert d["vector_score"] is None

    def test_splade_and_reranker_emit_none_when_absent(self) -> None:
        d = _serialize_search_result(_SearchHit())
        assert d["splade_score"] is None
        assert d["reranker_score"] is None

    def test_splade_and_reranker_rounded_when_present(self) -> None:
        d = _serialize_search_result(_SearchHit(splade_score=0.44444, reranker_score=0.99999))
        assert d["splade_score"] == 0.4444
        assert d["reranker_score"] == 1.0

    def test_result_missing_splade_attr_falls_back_to_none(self) -> None:
        """BaseSearchResult variants that predate splade/reranker fields."""

        class _Skinny:
            path = "/a"
            chunk_text = "x"
            score = 0.5
            chunk_index = 0
            line_start = None
            line_end = None
            keyword_score = None
            vector_score = None

        d = _serialize_search_result(_Skinny())
        assert d["splade_score"] is None
        assert d["reranker_score"] is None


# ---------------------------------------------------------------------------
# _compute_rebac_fetch_limit (#3701 — Issue 16A)
# ---------------------------------------------------------------------------


class TestComputeRebacFetchLimit:
    def test_returns_limit_unchanged_without_enforcer(self) -> None:
        assert _compute_rebac_fetch_limit(10, has_enforcer=False) == 10

    def test_applies_overfetch_factor_when_enforcer_present(self) -> None:
        assert _compute_rebac_fetch_limit(10, has_enforcer=True) == 10 * _REBAC_OVERFETCH_FACTOR

    def test_limit_one_scales_correctly(self) -> None:
        """Edge case: caller wanted a single result, enforcer active."""
        assert _compute_rebac_fetch_limit(1, has_enforcer=True) == _REBAC_OVERFETCH_FACTOR

    def test_zero_limit(self) -> None:
        """Zero request stays zero regardless of enforcer."""
        assert _compute_rebac_fetch_limit(0, has_enforcer=True) == 0
        assert _compute_rebac_fetch_limit(0, has_enforcer=False) == 0


# ---------------------------------------------------------------------------
# _rebac_denial_stats (#3701 — Issue 16A)
# ---------------------------------------------------------------------------


class TestRebacDenialStats:
    def test_no_denial_when_everything_permitted(self) -> None:
        stats = _rebac_denial_stats(pre_filter_count=10, post_filter_count=10, effective_limit=10)
        assert stats["permission_denial_rate"] == 0.0
        assert stats["truncated_by_permissions"] is False

    def test_low_denial_not_flagged_as_truncated(self) -> None:
        """25% denial, request fully satisfied."""
        stats = _rebac_denial_stats(pre_filter_count=40, post_filter_count=30, effective_limit=10)
        # Only 25% denial — below the warn threshold
        assert stats["permission_denial_rate"] == 0.25
        assert stats["truncated_by_permissions"] is False

    def test_high_denial_with_undercount_is_truncated(self) -> None:
        """75% denial AND fewer results than requested."""
        stats = _rebac_denial_stats(pre_filter_count=40, post_filter_count=5, effective_limit=10)
        assert stats["permission_denial_rate"] == 0.875
        assert stats["truncated_by_permissions"] is True

    def test_high_denial_with_enough_results_not_truncated(self) -> None:
        """Denial rate is high but we still got enough — no warning needed."""
        stats = _rebac_denial_stats(pre_filter_count=100, post_filter_count=20, effective_limit=10)
        # 80% denial but 20 >= 10 requested — not truncated
        assert stats["permission_denial_rate"] == 0.8
        assert stats["truncated_by_permissions"] is False

    def test_empty_pre_filter_zero_denial(self) -> None:
        """No results at all before filtering — avoid divide-by-zero."""
        stats = _rebac_denial_stats(pre_filter_count=0, post_filter_count=0, effective_limit=10)
        assert stats["permission_denial_rate"] == 0.0
        assert stats["truncated_by_permissions"] is False

    def test_denial_rate_rounded_to_4_decimals(self) -> None:
        stats = _rebac_denial_stats(pre_filter_count=3, post_filter_count=1, effective_limit=10)
        # 2/3 = 0.6666...
        assert stats["permission_denial_rate"] == 0.6667
