"""Unified search result types and field detection (Issue #1520, #1499).

Provides BaseSearchResult as the common base for all search result dataclasses,
eliminating 4x DRY violation across semantic.py, async_search.py, daemon.py,
and graph_retrieval.py.

Also provides detect_matched_field() — the canonical 6-field version used by
ranking.py and bm25s_search.py (Issue #1092, #1499).
"""

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

    @property
    def zone_qualified_path(self) -> str | None:
        """Path qualified with zone_id for cross-zone dedup.

        Returns '{zone_id}:{path}' when zone_id is set, None otherwise.
        Computed from zone_id + path so it can never drift out of sync.
        """
        return f"{self.zone_id}:{self.path}" if self.zone_id else None


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
