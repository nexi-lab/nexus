"""Shared indexed search requests."""

from dataclasses import dataclass

__all__ = [
    # SearchBrickProtocol.search bundled request (#4553 follow-up B)
    "SearchRequest",
    "split_path_scope",
    # Positional per-query failure marker for batch_search
    "BatchQueryFailure",
]


# =============================================================================
# SearchRequest — bundled call shape for SearchBrickProtocol.search
# =============================================================================
#
# ``search()`` had grown to 13 keyword arguments (query, search_type, limit,
# path_filter, alpha, fusion_method, rrf_k, zone_id, expand, adaptive_k,
# recency, recency_weight, recency_half_life_days) with the request-param
# feature set steadily growing (#4541 fusion, #4553 recency, #4545 title
# arm).  Every new knob shipped as another keyword and mock stubs sprouted
# matching kwargs.  Bundling into one frozen dataclass stops the accretion —
# new fields are pure data additions that don't churn signatures.
#
# The public ``search`` method takes exactly one argument: a
# ``SearchRequest``.  Private internal-to-daemon methods
# (``_search_on_current_loop`` etc.) keep individual kwargs — the boundary
# is drawn at the public contract, not repeated inside the module.
@dataclass(frozen=True, kw_only=True)
class SearchRequest:
    """Bundled parameters for ``SearchBrickProtocol.search``."""

    query: str
    # Kept as ``str`` (not ``Literal``) so callers passing an HTTP-validated
    # string variable don't have to cast — runtime validation lives at the
    # HTTP boundary in ``routers/search.py``.
    search_type: str = "hybrid"
    limit: int = 10
    path_filter: str | None = None
    # Extra path prefixes OR-ed with ``path_filter``: the plugin returns ONE
    # fused ranking over the union. Fused scores are only comparable within
    # one result list, so callers must not merge per-prefix queries by score.
    path_filters: tuple[str, ...] = ()
    alpha: float = 0.5
    fusion_method: str = "rrf"
    rrf_k: int = 60
    zone_id: str | None = None
    expand: str = "none"
    adaptive_k: bool = False
    recency: str | None = None
    recency_weight: float | None = None
    recency_half_life_days: float | None = None
    # Pre-computed query embedding. When set, the daemon uses this vector
    # for the dense leg instead of embedding the query text itself — the
    # batch endpoint embeds all unique query texts in one embed_batch call
    # and hands each inner search its vector.
    query_vector: list[float] | None = None
    # Batch-mode failure semantics. The interactive path degrades several
    # backend failures to an empty result (query timeout, legacy semantic
    # backend errors, missing query embedding on a semantic-only search).
    # When True those failures raise to the caller instead, so the batch
    # endpoint can report a per-query failure rather than a healthy empty.
    propagate_failures: bool = False
    # Set when a shared batch pre-embed already failed: dense legs skip
    # their own embed attempt (hybrid degrades to keyword-only immediately)
    # instead of re-hammering a degraded embedding provider once per query.
    embedding_unavailable: bool = False
    # Path-prefix score multipliers applied post-fusion by the plugin
    # (#4544 tier boost, rewired for P12 in #4620). Keys are in the
    # plugin's wire shape — slash-wrapped directory prefixes ("/docs/")
    # or "" for zone-wide — longest starts_with match wins. None/{} skips
    # the plugin's boost pass entirely. Built from the zone's
    # path_contexts rows by ``prefix_boosts_from_records``.
    path_prefix_boosts: dict[str, float] | None = None


def split_path_scope(
    paths: str | list[str] | tuple[str, ...] | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Normalise wire path prefix(es) to ``(path_filter, path_filters)``.

    One prefix stays on ``path_filter`` (unchanged single-scope behaviour);
    several go to ``path_filters`` for one fused ranking over the union.
    Blank entries are ignored and duplicates collapse, order-preserving.
    """
    if paths is None:
        return None, ()
    items = [paths] if isinstance(paths, str) else list(paths)
    unique = tuple(dict.fromkeys(p for p in items if p))
    if len(unique) <= 1:
        return (unique[0] if unique else None), ()
    return None, unique


@dataclass(frozen=True, kw_only=True)
class BatchQueryFailure:
    """Positional per-query failure marker returned by ``batch_search``.

    The batch endpoint historically collapsed inner exceptions to ``[]``,
    making a backend failure indistinguishable from a genuine empty result.
    Returning this marker instead lets callers with fail-closed coverage
    contracts (e.g. cross-workspace fan-out) count the query as FAILED
    rather than "searched, no matches".
    """

    error: str
