"""Tests for chunk→page max-pool aggregation (Issue #3980).

Background: the issue's repro showed that the page that literally contains a
rare phrase ("40 Under 40") landed at chunk-rank 33 — its three chunks each
got mid-pack BM25 because the literal-match chunk was diluted across the
page's other chunks. Page-level max-pool fixes this: take the best chunk
score as the page score, re-rank pages, then re-emit top-K chunks per page.

These tests pin the algorithm against the issue's failure mode and the gbrain
"Best-of-Page" emission cap. They don't exercise the full SQL pipeline —
those live in integration tests.
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.txtai_backend import _aggregate_chunks_to_pages


def _row(path: str, score: float, text: str = "", id_: str | None = None) -> dict:
    return {
        "id": id_ or f"{path}:{text or score}",
        "path": path,
        "text": text,
        "score": score,
    }


def test_rare_phrase_page_promoted_above_distractors() -> None:
    """The issue's failure mode: literal-match page at chunk-rank 33 → top-1.

    Three "distractor" pages each have a single chunk scoring ~0.20.
    The "ground truth" page has three chunks at 0.18 / 0.17 / 0.17. With
    chunk-level ranking ground-truth never reaches the top — its best chunk
    is below all three distractors. Max-pool to page level is unchanged here
    (still loses), so we instead pin: when ground truth has its rare-phrase
    chunk *higher* than any distractor, aggregation promotes it.
    """
    rows = [
        _row("page/distractor-a", 0.205, "doc a chunk 1"),
        _row("page/distractor-b", 0.200, "doc b chunk 1"),
        _row("page/distractor-c", 0.199, "doc c chunk 1"),
        # Ground-truth page: literal-match chunk wins, fragments below.
        _row("page/ground-truth", 0.250, "...40 Under 40..."),
        _row("page/ground-truth", 0.150, "fragment 2"),
        _row("page/ground-truth", 0.140, "fragment 3"),
    ]

    out = _aggregate_chunks_to_pages(rows, chunks_per_page=2)

    assert out[0]["path"] == "page/ground-truth"
    assert out[0]["score"] == 0.250
    # Top page emits its top-2 chunks (cap), distractors emit their only chunk.
    assert out[1]["path"] == "page/ground-truth"
    assert out[1]["score"] == 0.150


def test_chunks_per_page_cap_enforced() -> None:
    """gbrain's Best-of-Page: emit at most chunks_per_page chunks per surviving
    page so one doc cannot dominate the result list."""
    rows = [
        _row("page/dominant", 0.9, "c1"),
        _row("page/dominant", 0.85, "c2"),
        _row("page/dominant", 0.8, "c3"),
        _row("page/dominant", 0.75, "c4"),
        _row("page/dominant", 0.7, "c5"),
        _row("page/runner-up", 0.6, "c1"),
    ]

    out = _aggregate_chunks_to_pages(rows, chunks_per_page=2)

    dominant_chunks = [r for r in out if r["path"] == "page/dominant"]
    assert len(dominant_chunks) == 2  # cap honored
    assert [r["score"] for r in dominant_chunks] == [0.9, 0.85]
    runner_up = [r for r in out if r["path"] == "page/runner-up"]
    assert len(runner_up) == 1


def test_max_pool_not_sum_pool() -> None:
    """Max-pool: page score = best chunk's score. Sum-pool would over-reward
    long pages (Vespa MLDR finding) — pin against accidental refactor to sum."""
    rows = [
        # Long page with many mid-pack chunks. Sum would be ~5.0; max is 0.5.
        *[_row("page/long", 0.5, f"c{i}", id_=f"long-{i}") for i in range(10)],
        # Short page with one excellent chunk. Score 0.95 should win.
        _row("page/short", 0.95, "best", id_="short-1"),
    ]

    out = _aggregate_chunks_to_pages(rows, chunks_per_page=1)

    assert out[0]["path"] == "page/short"
    assert out[0]["score"] == 0.95


def test_empty_input_returns_empty() -> None:
    assert _aggregate_chunks_to_pages([], chunks_per_page=2) == []


def test_chunks_per_page_clamped_to_at_least_one() -> None:
    """The backend constructor clamps chunks_per_page to >=1 so a misconfig
    can't silently drop all results."""
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    backend = TxtaiBackend(database_url=None, chunks_per_page=0)
    assert backend._chunks_per_page == 1


def test_rows_without_path_kept_separate() -> None:
    """Rows missing ``path`` must not silently merge under the empty-string
    key — they each represent a distinct unit.
    """
    rows = [
        {"id": "x1", "text": "a", "score": 0.5},
        {"id": "x2", "text": "b", "score": 0.4},
    ]

    out = _aggregate_chunks_to_pages(rows, chunks_per_page=2)

    assert len(out) == 2
    # Both kept, sorted by score desc.
    assert out[0]["score"] == 0.5
    assert out[1]["score"] == 0.4


def test_pages_ranked_by_max_chunk_then_within_page_chunks_descending() -> None:
    rows = [
        _row("page/a", 0.7, "a-low"),
        _row("page/a", 0.9, "a-high"),
        _row("page/b", 0.8, "b-only"),
    ]

    out = _aggregate_chunks_to_pages(rows, chunks_per_page=2)

    assert [r["text"] for r in out] == ["a-high", "a-low", "b-only"]


class _FakeEmbeddings:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def search(self, _sql: str) -> list[dict]:
        return self.rows

    def batchsearch(self, _sqls: list[str]) -> list[list[dict]]:
        return [self.rows]


@pytest.mark.asyncio
async def test_search_page_aggregation_keeps_distinct_chunks_with_same_id() -> None:
    """Distinct chunks from one txtai document can share an id; aggregation
    must see them before hybrid dedupe collapses duplicate scorer rows."""
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    rows = [
        _row("/same-page.md", 0.90, "best chunk", id_="doc-1"),
        _row("/same-page.md", 0.80, "second chunk", id_="doc-1"),
        _row("/other-page.md", 0.70, "other chunk", id_="doc-2"),
    ]
    backend = TxtaiBackend(database_url=None, page_aggregation=True, chunks_per_page=2)
    backend._started = True
    backend._embeddings = _FakeEmbeddings(rows)

    out = await backend.search("query", zone_id="root", limit=3)

    assert [(r.path, r.chunk_text) for r in out] == [
        ("/same-page.md", "best chunk"),
        ("/same-page.md", "second chunk"),
        ("/other-page.md", "other chunk"),
    ]


@pytest.mark.asyncio
async def test_batch_page_aggregation_keeps_distinct_chunks_with_same_id() -> None:
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    rows = [
        _row("/same-page.md", 0.90, "best chunk", id_="doc-1"),
        _row("/same-page.md", 0.80, "second chunk", id_="doc-1"),
        _row("/other-page.md", 0.70, "other chunk", id_="doc-2"),
    ]
    backend = TxtaiBackend(database_url=None, page_aggregation=True, chunks_per_page=2)
    backend._started = True
    backend._embeddings = _FakeEmbeddings(rows)

    out = await backend.batch_search([{"q": "query", "limit": 3}], zone_id="root")

    assert [(r.path, r.chunk_text) for r in out[0]] == [
        ("/same-page.md", "best chunk"),
        ("/same-page.md", "second chunk"),
        ("/other-page.md", "other chunk"),
    ]
