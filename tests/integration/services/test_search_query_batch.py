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

import pytest

try:
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
            ({"q": "x", "limit": 0}, "limit"),
            ({"q": "x", "limit": 101}, "limit"),
            ({"q": "x", "limit": "ten"}, "limit"),
            ({"q": "x", "alpha": 1.5}, "alpha"),
            ({"q": "x", "alpha": -0.1}, "alpha"),
            ({"q": "x", "rrf_k": 0}, "rrf_k"),
            ({"q": "x", "rrf_k": 1001}, "rrf_k"),
            ({"q": "x", "recency_weight": 5.1}, "recency_weight"),
            ({"q": "x", "recency_half_life_days": 0}, "recency_half_life_days"),
            ({"q": "x", "recency_half_life_days": 3651}, "recency_half_life_days"),
            ({"q": "x", "path": 42}, "path"),
        ],
    )
    def test_invalid_specs_return_error_message(self, raw, fragment):
        err = parse_batch_query_spec(raw)
        assert isinstance(err, str)
        assert fragment in err

    def test_spec_query_text_best_effort(self):
        assert spec_query_text({"q": "a"}) == "a"
        assert spec_query_text({"query": "b"}) == "b"
        assert spec_query_text({"q": "a", "query": "b"}) == "a"
        assert spec_query_text("junk") == ""
        assert spec_query_text({}) == ""
