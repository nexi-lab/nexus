from nexus.bricks.search.macro_chunk import (
    ChunkRow,
    ExpansionConfig,
    _is_code_path,
    _section_bounds,
    _stitch,
    _window_for_anchor,
    merge_spans,
)


def _row(idx, tokens=10, heading="H1", text=None, ls=None, le=None, path="/a.md"):
    return ChunkRow(
        path=path,
        chunk_index=idx,
        text=text or f"c{idx}",
        tokens=tokens,
        line_start=ls,
        line_end=le,
        heading_prefix=heading,
    )


def _map(rows):
    return {r.chunk_index: r for r in rows}


def test_merge_spans_collapses_overlapping_and_adjacent():
    spans = [("/a", 0, 5), ("/a", 6, 9), ("/a", 20, 22), ("/b", 0, 3)]
    out = sorted(merge_spans(spans))
    assert out == [("/a", 0, 9), ("/a", 20, 22), ("/b", 0, 3)]


def test_is_code_path():
    assert _is_code_path("/x/foo.py") is True
    assert _is_code_path("/x/foo.md") is False


def test_section_bounds_stops_at_heading_change():
    rows = [_row(0, heading="A"), _row(1, heading="A"), _row(2, heading="B"), _row(3, heading="B")]
    assert _section_bounds(_map(rows), 1) == (0, 1)
    assert _section_bounds(_map(rows), 2) == (2, 3)


def test_window_whole_section_when_under_budget():
    rows = [_row(i, tokens=10, heading="A") for i in range(4)]
    # section tokens = 40 <= budget 1024 -> whole section
    lo, hi = _window_for_anchor(_map(rows), 2, ExpansionConfig(token_budget=1024), False)
    assert (lo, hi) == (0, 3)


def test_window_prose_centered_when_over_budget():
    rows = [_row(i, tokens=100, heading="A") for i in range(7)]
    # budget 250 -> anchor(3)=100, then centered: 2,4 -> 300>250 stop at 250
    lo, hi = _window_for_anchor(_map(rows), 3, ExpansionConfig(token_budget=250), False)
    assert lo <= 3 <= hi
    assert sum(rows[i].tokens for i in range(lo, hi + 1)) <= 250
    assert (hi - lo) >= 1  # expanded beyond the single anchor


def test_window_code_forward_bias():
    rows = [_row(i, tokens=100, heading="A", path="/f.py") for i in range(7)]
    lo, hi = _window_for_anchor(_map(rows), 3, ExpansionConfig(token_budget=250), True)
    # forward-first: should include 4 before 2
    assert hi >= 4
    assert sum(rows[i].tokens for i in range(lo, hi + 1)) <= 250


def test_stitch_concats_and_spans_lines():
    rows = [_row(0, text="alpha", ls=1, le=2), _row(1, text="beta", ls=3, le=5)]
    text, ls, le = _stitch(_map(rows), 0, 1)
    assert text == "alpha\nbeta"
    assert (ls, le) == (1, 5)
