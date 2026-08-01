"""Tests for BaseSearchResult serialization (#4398)."""

from nexus.bricks.search.results import BaseSearchResult
from nexus.server.api.v2.routers.search import _serialize_search_result


def test_serialize_omits_macro_when_absent():
    """Default response unchanged when macro_text is absent."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    out = _serialize_search_result(r)
    assert "macro_text" not in out


def test_serialize_includes_macro_when_present():
    """Macro fields included in response when macro_text is set."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    r.macro_text = "x\ny"
    r.macro_line_start = 1
    r.macro_line_end = 4
    out = _serialize_search_result(r)
    assert out["macro_text"] == "x\ny"
    assert out["macro_line_start"] == 1
    assert out["macro_line_end"] == 4


def test_serialize_omits_title_score_when_absent():
    """Default response unchanged when title_score is absent (#4545)."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    out = _serialize_search_result(r)
    assert "title_score" not in out


def test_serialize_includes_title_score_when_present():
    """title_score emitted rounded to 4 places when the title arm scored the hit."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    r.title_score = 7.00004
    out = _serialize_search_result(r)
    assert out["title_score"] == 7.0
