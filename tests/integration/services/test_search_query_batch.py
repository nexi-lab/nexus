"""Batch search query spec parser tests."""

from __future__ import annotations

import sys
import types

# nexus.bricks.search.__init__ imports SearchService → nexus_runtime (Rust
# extension).  The Rust binary is not available in the test venv, so we
# stub the module before any nexus.bricks.search import can trigger it.
# Using a MagicMock stub so that any attribute access (import name from ...)
# succeeds without enumerating every symbol the Rust extension exposes.
if "nexus_runtime" not in sys.modules:
    from unittest.mock import MagicMock as _MagicMock

    _nexus_runtime_stub = _MagicMock()
    _nexus_runtime_stub.__name__ = "nexus_runtime"
    _nexus_runtime_stub.__spec__ = types.ModuleType("nexus_runtime")
    sys.modules["nexus_runtime"] = _nexus_runtime_stub

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock  # noqa: F401

import pytest

try:
    from fastapi import FastAPI  # noqa: F401
    from fastapi.testclient import TestClient  # noqa: F401

    _HAS_FASTAPI_TESTCLIENT = True
except ImportError:
    _HAS_FASTAPI_TESTCLIENT = False

from nexus.server.api.v2.routers._search_batch import (
    ParsedBatchSpec,
    parse_batch_query_spec,
    spec_query_text,
)


@dataclass
class _MockResult:
    path: str = "test.txt"
    chunk_text: str = "hello"
    score: float = 0.95
    chunk_index: int = 0
    line_start: int | None = None
    line_end: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    splade_score: float | None = None
    reranker_score: float | None = None


class TestParseBatchQuerySpec:
    def test_defaults(self):
        spec = parse_batch_query_spec({"q": "hello"})
        assert spec == ParsedBatchSpec(query="hello")
        assert (spec.search_type, spec.limit, spec.alpha) == ("hybrid", 10, 0.5)
        assert (spec.fusion_method, spec.rrf_k, spec.expand) == ("rrf", 60, "none")
        assert (spec.recency, spec.recency_weight, spec.recency_half_life_days) == (
            None,
            None,
            None,
        )

    def test_public_names_win_over_legacy_aliases(self):
        spec = parse_batch_query_spec(
            {
                "q": "public",
                "query": "legacy",
                "type": "semantic",
                "search_type": "keyword",
                "path": "/a/",
                "path_filter": "/b/",
                "fusion": "weighted",
                "fusion_method": "rrf",
            }
        )
        assert isinstance(spec, ParsedBatchSpec)
        assert spec.query == "public"
        assert spec.search_type == "semantic"
        assert spec.path_filter == "/a/"
        assert spec.fusion_method == "weighted"

    def test_legacy_aliases_still_accepted(self):
        spec = parse_batch_query_spec(
            {"query": "legacy", "search_type": "keyword", "path_filter": "/b/"}
        )
        assert isinstance(spec, ParsedBatchSpec)
        assert (spec.query, spec.search_type, spec.path_filter) == ("legacy", "keyword", "/b/")

    def test_tuning_params_parsed(self):
        spec = parse_batch_query_spec(
            {
                "q": "tuned",
                "alpha": 0.3,
                "fusion": "weighted",
                "rrf_k": 90,
                "expand": "macro",
                "recency": "on",
                "recency_weight": 1.5,
                "recency_half_life_days": 30,
            }
        )
        assert isinstance(spec, ParsedBatchSpec)
        assert (spec.alpha, spec.fusion_method, spec.rrf_k) == (0.3, "weighted", 90)
        assert (spec.expand, spec.recency) == ("macro", "on")
        assert (spec.recency_weight, spec.recency_half_life_days) == (1.5, 30.0)

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            ("not a dict", "expected an object"),
            ({}, "query text"),
            ({"q": ""}, "query text"),
            # Structured / null / non-string query text must error, never be
            # stringified into a searchable Python repr.
            ({"q": {"nested": "value"}}, "query text"),
            ({"q": None}, "query text"),
            ({"q": 42}, "query text"),
            # Public name wins by KEY PRESENCE: an explicit empty "q" must
            # error rather than silently executing the legacy alias.
            ({"q": "", "query": "legacy"}, "query text"),
            ({"q": "x", "limit": 0}, "limit"),
            ({"q": "x", "limit": 101}, "limit"),
            ({"q": "x", "limit": "ten"}, "limit"),
            # Fractional numerics must not silently truncate (single-route
            # pydantic rejects them); integral floats stay accepted.
            ({"q": "x", "limit": 1.9}, "limit"),
            ({"q": "x", "rrf_k": 60.5}, "rrf_k"),
            ({"q": "x", "alpha": 1.5}, "alpha"),
            ({"q": "x", "alpha": -0.1}, "alpha"),
            ({"q": "x", "rrf_k": 0}, "rrf_k"),
            ({"q": "x", "rrf_k": 1001}, "rrf_k"),
            ({"q": "x", "recency_weight": 5.1}, "recency_weight"),
            ({"q": "x", "recency_half_life_days": 0}, "recency_half_life_days"),
            ({"q": "x", "recency_half_life_days": 3651}, "recency_half_life_days"),
            ({"q": "x", "path": 42}, "path"),
            ({"q": "x", "recency_half_life_days": "nan"}, "recency_half_life_days"),
            ({"q": "x", "recency_half_life_days": float("nan")}, "recency_half_life_days"),
            ({"q": "x", "type": "keywrod"}, "type"),
            # Explicit falsy enum values must error, not silently coerce to
            # the default semantics (the single route rejects them too).
            ({"q": "x", "type": ""}, "type"),
            ({"q": "x", "type": None}, "type"),
            ({"q": "x", "fusion": ""}, "fusion"),
            ({"q": "x", "expand": ""}, "expand"),
            ({"q": "x", "recency": ""}, "recency"),
            # Legacy alias resolves first; the message reports the canonical
            # public name.
            ({"q": "x", "search_type": "keywrod"}, "type"),
            ({"q": "x", "fusion": "bogus"}, "fusion"),
            ({"q": "x", "expand": "huge"}, "expand"),
            ({"q": "x", "recency": "always"}, "recency"),
        ],
    )
    def test_invalid_specs_return_error_message(self, raw, fragment):
        err = parse_batch_query_spec(raw)
        assert isinstance(err, str)
        assert fragment in err

    def test_recency_none_or_absent_stays_valid(self):
        # recency is the only optional enum: absent AND explicit null both
        # mean "no recency directive" and must not trip enum validation.
        absent = parse_batch_query_spec({"q": "x"})
        assert isinstance(absent, ParsedBatchSpec)
        assert absent.recency is None
        explicit_null = parse_batch_query_spec({"q": "x", "recency": None})
        assert isinstance(explicit_null, ParsedBatchSpec)
        assert explicit_null.recency is None

    def test_spec_query_text_best_effort(self):
        assert spec_query_text({"q": "a"}) == "a"
        assert spec_query_text({"query": "b"}) == "b"
        assert spec_query_text({"q": "a", "query": "b"}) == "a"
        assert spec_query_text("junk") == ""
        assert spec_query_text({}) == ""
        # Presence beats truthiness: explicit "q" masks the legacy alias
        # even when falsy, and non-string values echo as "".
        assert spec_query_text({"q": "", "query": "b"}) == ""
        assert spec_query_text({"q": {"nested": "v"}}) == ""
        assert spec_query_text({"q": None, "query": "b"}) == ""

    def test_integral_float_limit_accepted(self):
        spec = parse_batch_query_spec({"q": "x", "limit": 5.0})
        assert isinstance(spec, ParsedBatchSpec)
        assert spec.limit == 5


def _build_batch_app(mock_daemon):
    from fastapi import FastAPI

    from nexus.server.api.v2.routers.search import router

    app = FastAPI()
    app.include_router(router)
    mock_daemon.is_initialized = True
    app.state.search_daemon = mock_daemon
    app.state.search_daemon_enabled = True
    app.state.record_store = MagicMock()
    app.state.async_session_factory = MagicMock()
    app.state.async_read_session_factory = MagicMock()

    from nexus.server.dependencies import require_auth

    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "user_id": "u",
        "zone_id": "eng",
        "zone_set": ["eng"],
        "zone_perms": [["eng", "r"]],
    }
    return app


@pytest.mark.skipif(not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available")
class TestBatchRoute:
    def test_params_forwarded_to_daemon(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[[]])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            json={
                "queries": [
                    {
                        "q": "tuned",
                        "type": "hybrid",
                        "limit": 7,
                        "path": "/ws/",
                        "alpha": 0.3,
                        "fusion": "weighted",
                        "rrf_k": 90,
                        "expand": "macro",
                        "recency": "on",
                        "recency_weight": 1.5,
                        "recency_half_life_days": 30,
                    }
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        (specs,) = daemon.batch_search.call_args.args
        assert daemon.batch_search.call_args.kwargs == {"zone_id": "eng"}
        spec = specs[0]
        assert spec["query"] == "tuned"
        assert spec["search_type"] == "hybrid"
        assert spec["path_filter"] == "/ws/"
        assert (spec["alpha"], spec["fusion_method"], spec["rrf_k"]) == (0.3, "weighted", 90)
        assert (spec["expand"], spec["recency"]) == ("macro", "on")
        assert (spec["recency_weight"], spec["recency_half_life_days"]) == (1.5, 30.0)
        # No enforcer on this app -> fetch limit == requested limit.
        assert spec["limit"] == 7

    def test_serializer_parity_with_single_query(self):
        from nexus.server.api.v2.routers._search_serialize import _serialize_search_result

        result = _MockResult(
            path="/ws/doc.md",
            chunk_text="hello",
            score=0.9123456,
            chunk_index=3,
            line_start=10,
            line_end=14,
            keyword_score=1.25,
            vector_score=0.75,
        )
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[[result]])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch", json={"queries": [{"q": "hello"}]}
        )

        assert resp.status_code == 200, resp.text
        entry = resp.json()["queries"][0]
        assert "error" not in entry
        assert entry["total"] == 1
        assert entry["results"][0] == _serialize_search_result(result)
        assert entry["results"][0]["chunk_index"] == 3
        assert entry["results"][0]["line_start"] == 10

    def test_daemon_failure_becomes_error_entry(self):
        from nexus.contracts.search_types import BatchQueryFailure

        daemon = MagicMock()
        daemon.batch_search = AsyncMock(
            return_value=[[], BatchQueryFailure(error="backend fell over")]
        )
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            json={"queries": [{"q": "ok"}, {"q": "doomed"}]},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body["queries"][0]
        assert body["queries"][1] == {
            "query": "doomed",
            "results": [],
            "total": 0,
            "error": "backend fell over",
        }

    def test_invalid_spec_gets_error_entry_and_is_not_sent_to_daemon(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[[]])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            json={"queries": [{"q": "bad-limit", "limit": 0}, {"q": "fine"}]},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_queries"] == 2
        assert body["queries"][0]["query"] == "bad-limit"
        assert "limit" in body["queries"][0]["error"]
        assert body["queries"][0]["results"] == []
        assert "error" not in body["queries"][1]
        # Only the valid spec reached the daemon.
        (specs,) = daemon.batch_search.call_args.args
        assert [s["query"] for s in specs] == ["fine"]

    def test_all_specs_invalid_skips_daemon_entirely(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch", json={"queries": [{"q": "x", "limit": 9999}]}
        )

        assert resp.status_code == 200, resp.text
        assert "limit" in resp.json()["queries"][0]["error"]
        daemon.batch_search.assert_not_called()

    @pytest.mark.parametrize("body", [[], None, "abc", 42])
    def test_non_object_json_root_400(self, body):
        import json as json_module

        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        # Post raw content: httpx treats json=None as "no body", but the
        # contract under test is a request whose JSON root is null/[]/str/int.
        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            content=json_module.dumps(body),
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 400, resp.text
        daemon.batch_search.assert_not_called()

    @pytest.mark.parametrize("content", ["", "{not json", '{"queries": [truncated'])
    def test_malformed_json_body_400(self, content):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            content=content,
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 400, resp.text
        daemon.batch_search.assert_not_called()

    def test_empty_queries_still_400(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post("/api/v2/search/query/batch", json={"queries": []})

        assert resp.status_code == 400

    @pytest.mark.parametrize("queries", ["ab", 42])
    def test_non_list_queries_400(self, queries):
        # A string used to iterate per-char into per-entry errors (200) and
        # an int 500'd in the comprehension; both are batch-level 400s.
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post("/api/v2/search/query/batch", json={"queries": queries})

        assert resp.status_code == 400
        daemon.batch_search.assert_not_called()
