import pytest

from nexus.bricks.search.macro_chunk import ChunkRow, ExpansionConfig, expand_results


class _Result:
    def __init__(self, path, chunk_index):
        self.path = path
        self.chunk_index = chunk_index
        self.macro_text = None
        self.macro_line_start = None
        self.macro_line_end = None


class _FakeFetcher:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def fetch_ranges(self, spans, zone_id):
        self.calls.append(list(spans))
        wanted = list(spans)
        out = []
        for r in self._rows:
            for path, lo, hi in wanted:
                if r.path == path and lo <= r.chunk_index <= hi:
                    out.append(r)
                    break
        return out


def _row(idx, path="/a.md", heading="A", tokens=10, text=None, ls=None, le=None):
    return ChunkRow(
        path=path,
        chunk_index=idx,
        text=text or f"c{idx}",
        tokens=tokens,
        line_start=ls,
        line_end=le,
        heading_prefix=heading,
    )


@pytest.mark.asyncio
async def test_expand_attaches_macro_text_for_section():
    rows = [_row(i, heading="A", ls=i + 1, le=i + 1) for i in range(3)]
    fetcher = _FakeFetcher(rows)
    res = [_Result("/a.md", 1)]
    out = await expand_results(res, fetcher, ExpansionConfig(token_budget=1024, window=8))
    assert out[0].macro_text == "c0\nc1\nc2"
    assert (out[0].macro_line_start, out[0].macro_line_end) == (1, 3)


@pytest.mark.asyncio
async def test_expand_single_batched_fetch_and_section_dedup():
    rows = [_row(i, heading="A") for i in range(4)]
    fetcher = _FakeFetcher(rows)
    # two hits in the SAME section
    res = [_Result("/a.md", 1), _Result("/a.md", 2)]
    out = await expand_results(res, fetcher, ExpansionConfig(token_budget=1024, window=8))
    assert len(fetcher.calls) == 1  # one batched fetch
    assert out[0].macro_text == out[1].macro_text  # section computed once, shared


@pytest.mark.asyncio
async def test_expand_missing_anchor_leaves_result_untouched():
    fetcher = _FakeFetcher([])  # eventual-consistency gap: nothing returned
    res = [_Result("/a.md", 5)]
    out = await expand_results(res, fetcher, ExpansionConfig())
    assert out[0].macro_text is None  # never errors, no expansion


@pytest.mark.asyncio
async def test_expand_empty_results_noop():
    fetcher = _FakeFetcher([])
    assert await expand_results([], fetcher, ExpansionConfig()) == []
