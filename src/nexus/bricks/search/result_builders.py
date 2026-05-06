"""Shared result-dict construction helpers for search backends (Issue #1520).

Extracts the duplicated result-dict construction from vector_db.py,
vector_db_sqlite.py, and vector_db_postgres.py into one canonical location.

All search backends produce dicts with the same shape; this module provides
build_semantic_result() to eliminate the 5x duplication.
"""

from typing import Any


def build_semantic_result(
    *,
    chunk_id: str | None,
    path: str,
    chunk_index: int,
    chunk_text: str,
    start_offset: int | None,
    end_offset: int | None,
    line_start: int | None,
    line_end: int | None,
    score: float,
    keyword_score: float | None = None,
    vector_score: float | None = None,
) -> dict[str, Any]:
    """Build a canonical search result dict.

    This is the single source of truth for the result dict shape used
    throughout the search brick. All vector_search, keyword_search, and
    hybrid_search methods should use this builder instead of inline dicts.

    Args:
        chunk_id: Chunk identifier (None for Zoekt/BM25S results).
        path: Virtual file path.
        chunk_index: Index of chunk within document.
        chunk_text: Chunk text content.
        start_offset: Start byte offset in document.
        end_offset: End byte offset in document.
        line_start: Start line number.
        line_end: End line number.
        score: Relevance score (higher = better).
        keyword_score: Keyword component score (optional).
        vector_score: Vector component score (optional).

    Returns:
        Canonical search result dict.
    """
    result: dict[str, Any] = {
        "chunk_id": chunk_id,
        "path": path,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "line_start": line_start,
        "line_end": line_end,
        "score": score,
    }
    if keyword_score is not None:
        result["keyword_score"] = keyword_score
    if vector_score is not None:
        result["vector_score"] = vector_score
    return result


def build_result_from_row(row: Any, *, score_abs: bool = False) -> dict[str, Any]:
    """Build a result dict from a SQLAlchemy row.

    Expects the row to have: chunk_id, virtual_path, chunk_index,
    chunk_text, start_offset, end_offset, line_start, line_end, score.

    Args:
        row: SQLAlchemy result row.
        score_abs: If True, take abs(score) (for FTS5/BM25 negative ranks).

    Returns:
        Canonical search result dict.
    """
    raw_score = float(row.score)
    return build_semantic_result(
        chunk_id=row.chunk_id,
        path=row.virtual_path,
        chunk_index=row.chunk_index,
        chunk_text=row.chunk_text,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        line_start=row.line_start,
        line_end=row.line_end,
        score=abs(raw_score) if score_abs else raw_score,
    )


def _aggregate_chunks_to_pages(
    chunks: list[dict[str, Any]],
    *,
    chunks_per_page: int,
) -> list[dict[str, Any]]:
    """Max-pool chunk scores to page (path) granularity, re-rank pages, emit
    the top-K best chunks per surviving page.

    Why this exists (Issue #3980): txtai's pgtext BM25 scores at chunk
    granularity. For rare-phrase queries the chunk that literally contains the
    phrase loses on local BM25 — a 360-word page split into three 120-word
    chunks distributes term frequency across chunks, and a competing page
    whose single chunk repeats a frequent token can outscore it. The page
    that *should* rank in the top-5 ends up at chunk-rank 30+ across its
    fragments. Page-level max-pool restores the right ranking with the
    bonus that we keep chunk-grain context for downstream RAG.

    Pattern matches Vespa's MaxSim long-context evaluation, ColBERT's
    max-then-sum late-interaction, and gbrain's "Best-of-Page" dedup.

    Args:
        chunks: deduped raw rows from the txtai SQL search. Each row must
            carry ``path`` and ``score``; rows missing ``path`` get treated
            as their own page (``id`` fallback) so they're not silently
            collapsed together.
        chunks_per_page: cap on chunks emitted per page. Protects against
            one-doc dominance at the top of the result list.

    Returns:
        A flat list of chunk rows, ordered by (page rank desc, chunk score
        desc within page). Length is at most ``chunks_per_page * len(pages)``.
    """
    if not chunks:
        return []

    by_page: dict[str, list[dict[str, Any]]] = {}
    for r in chunks:
        # Use ``path`` as the page key. Fall back to ``id`` (and finally to a
        # synthetic per-row key) so rows without a path don't collide on the
        # empty string and silently merge.
        path = r.get("path") or ""
        key = path or str(r.get("id") or id(r))
        by_page.setdefault(key, []).append(r)

    ranked_pages: list[tuple[float, list[dict[str, Any]]]] = []
    for page_chunks in by_page.values():
        # Max-pool: page score = best chunk's score. Sum-pool conflates
        # chunk count with relevance and over-rewards long pages; max keeps
        # the rare-phrase signal intact.
        page_chunks.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
        page_score = float(page_chunks[0].get("score", 0.0))
        ranked_pages.append((page_score, page_chunks))

    ranked_pages.sort(key=lambda item: item[0], reverse=True)

    out: list[dict[str, Any]] = []
    for _page_score, page_chunks in ranked_pages:
        out.extend(page_chunks[:chunks_per_page])
    return out
