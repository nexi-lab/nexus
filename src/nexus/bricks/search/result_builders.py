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
