"""Shared search result types for transport and federation."""

from collections.abc import Iterable
from dataclasses import dataclass

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


@dataclass
class BaseSearchResult:
    """Common search result fields shared by all search types.

    Search clients and cross-zone response adapters share these fields.
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
    semantic_degraded: bool | None = None
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
    # Issue #4130 review R7: LLM query-expansion attribution — which arm
    # of the fused fan-out surfaced this hit.  0 = the ORIGINAL query
    # text; 1..N = LLM variant N (1-based).  Absent (None) when the hit
    # did NOT flow through the plugin's expansion wrapper (expansion
    # disabled / single-shot fallback / peer-fanout hit).
    expansion_variant_index: int | None = None

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
