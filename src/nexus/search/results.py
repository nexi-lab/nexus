"""Unified search result types (Issue #1520).

Provides BaseSearchResult as the common base for all search result dataclasses,
eliminating 4x DRY violation across semantic.py, async_search.py, daemon.py,
and graph_retrieval.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BaseSearchResult:
    """Common search result fields shared by all search types.

    All search result dataclasses in the search brick extend this base.
    This enables fuse_results() to accept typed results directly instead
    of requiring dict conversion.
    """

    path: str
    chunk_text: str
    score: float
    chunk_index: int = 0
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
