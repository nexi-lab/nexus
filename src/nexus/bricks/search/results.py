"""Unified search result types and field detection (Issue #1520, #1499).

Provides BaseSearchResult as the common base for all search result dataclasses,
eliminating 4x DRY violation across semantic.py, async_search.py, daemon.py,
and graph_retrieval.py.

Also provides detect_matched_field() — the canonical 6-field version used by
ranking.py and bm25s_search.py (Issue #1092, #1499).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Backend timing legs surfaced on ``SearchResultList.search_timing`` and
# echoed by the ``/query`` router in its response envelope.  Federated
# search sums per-peer legs into the aggregate under the same keys.
BACKEND_LEG_TIMING_KEYS = (
    "backend_ms",
    "embed_ms",
    "keyword_ms",
    "page_keyword_ms",
    "title_ms",
    "vector_ms",
    "fusion_ms",
    "rerank_ms",
    "index_load_ms",
    "fallback_ms",
)


# Tier-boost over-fetch cap (Issue #4544): env-sourced ranking knobs are
# untrusted input; the widening factor is capped so route-limit × ReBAC
# over-fetch × this cap keeps backend work bounded.
TIER_BOOST_OVERFETCH_CAP = 10


def sane_overfetch_factor(raw: Any) -> int:
    """Clamp the tier-boost over-fetch factor to ``[1, TIER_BOOST_OVERFETCH_CAP]``."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, TIER_BOOST_OVERFETCH_CAP))


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
    splade_score: float | None = None  # SPLADE learned sparse score
    reranker_score: float | None = None  # Cross-encoder reranker score
    # Issue #1092: Attribute ranking metadata (merged from SemanticSearchResult)
    matched_field: str | None = None  # Which field matched (filename, path, content, etc.)
    attribute_boost: float | None = None  # Boost multiplier applied
    original_score: float | None = None  # Score before attribute boosting
    # Issue #3147: Federated search — zone provenance
    zone_id: str | None = None  # Source zone for cross-zone federated results
    # Issue #3773: admin-configured path description for LLM consumers
    context: str | None = None
    semantic_degraded: bool | None = None  # Issue #3778: federation fell back to BM25S
    # Issue #4398: macro-chunk expansion fields for hybrid search context
    macro_text: str | None = None
    macro_line_start: int | None = None
    macro_line_end: int | None = None
    # Issue #4544: configured per-prefix source-tier weight whose policy was
    # applied to score (None = unboosted). The transform is signed-safe:
    # non-negative scores multiplied by the weight, negative scores divided
    # by it — so the pre-boost score is score/tier_boost when score >= 0
    # and score*tier_boost when score < 0.
    tier_boost: float | None = None
    # Issue #4545: skeleton title-arm attribution — locate() score when the
    # title arm voted for this result in hybrid fusion, else None.
    title_score: float | None = None
    # Issue #4543: recency-decay attribution — the multiplier applied to
    # ``score`` when the post-fusion recency boost fired; None otherwise.
    recency_boost: float | None = None

    @property
    def zone_qualified_path(self) -> str | None:
        """Path qualified with zone_id for cross-zone dedup.

        Returns '{zone_id}:{path}' when zone_id is set, None otherwise.
        Computed from zone_id + path so it can never drift out of sync.
        """
        return f"{self.zone_id}:{self.path}" if self.zone_id else None


class SearchResultList(list[BaseSearchResult]):
    """``list[BaseSearchResult]`` plus request-level search-timing snapshot.

    ``semantic_degraded`` (#3778) carries request-level degradation so a
    federated response whose result list is *empty* still surfaces the
    signal — per-result stamping alone loses it when there are no
    results.  ``search_timing`` holds the per-leg backend phase timings
    keyed by :data:`BACKEND_LEG_TIMING_KEYS`.
    """

    semantic_degraded: bool = False

    def __init__(
        self,
        results: Iterable[BaseSearchResult] = (),
        *,
        search_timing: dict[str, float] | None = None,
    ) -> None:
        super().__init__(results)
        self.search_timing = dict(search_timing or {})


def detect_matched_field(
    query: str,
    path: str,
    content: str | None = None,  # noqa: ARG001 - kept for API consistency
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Detect which field the query primarily matched in.

    Checks fields in order of importance (filename first, content last)
    and returns the first field where a match is found.

    Args:
        query: Search query
        path: File path
        content: File content (optional, reserved for API consistency)
        title: Document title (optional)
        tags: Document tags (optional)
        description: Document description (optional)

    Returns:
        Name of the matched field ("filename", "title", "path", "tags", "description", "content")
    """
    query_lower = query.lower().strip()
    query_terms = query_lower.split()

    # Extract filename from path
    filename = path.split("/")[-1].lower() if path else ""
    filename_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Check filename (highest priority)
    if query_lower in filename or query_lower in filename_without_ext:
        return "filename"

    # Check if all query terms appear in filename
    if all(term in filename for term in query_terms):
        return "filename"

    # Check title
    if title:
        title_lower = title.lower()
        if query_lower in title_lower or all(term in title_lower for term in query_terms):
            return "title"

    # Check tags
    if tags:
        tags_lower = [t.lower() for t in tags]
        tags_combined = " ".join(tags_lower)
        if query_lower in tags_combined or any(query_lower in t for t in tags_lower):
            return "tags"

    # Check path (excluding filename)
    path_lower = path.lower() if path else ""
    path_without_filename = "/".join(path_lower.split("/")[:-1]) if "/" in path_lower else ""
    if query_lower in path_without_filename:
        return "path"

    # Check description
    if description:
        desc_lower = description.lower()
        if query_lower in desc_lower:
            return "description"

    # Default to content
    return "content"
