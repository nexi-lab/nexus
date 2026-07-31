"""Search response shaping helpers (#3701 review — Issue 5A).

Extracted from ``routers/search.py`` when the router crossed the repo's
2000-line file cap (#4545 rebase). The router re-exports
``_serialize_search_result`` so existing import paths keep working.
"""

from __future__ import annotations

from typing import Any


def _serialize_search_result(result: Any) -> dict[str, Any]:
    """Serialize a single search result into the canonical response dict.

    Collapses the 25-line dict comprehension previously duplicated across
    the graph and non-graph branches of ``search_query``. Preserves the
    pre-refactor field ordering, rounding, and None semantics.

    Issue #3773: emits ``context`` when the result carries a non-None value
    (omits the key otherwise to keep responses compact).
    """
    out: dict[str, Any] = {
        "path": result.path,
        "chunk_text": result.chunk_text,
        "score": round(result.score, 4),
        "chunk_index": result.chunk_index,
        "line_start": result.line_start,
        "line_end": result.line_end,
        "keyword_score": (round(result.keyword_score, 4) if result.keyword_score else None),
        "vector_score": (round(result.vector_score, 4) if result.vector_score else None),
    }
    splade = getattr(result, "splade_score", None)
    out["splade_score"] = round(splade, 4) if splade is not None else None
    reranker = getattr(result, "reranker_score", None)
    out["reranker_score"] = round(reranker, 4) if reranker is not None else None
    title = getattr(result, "title_score", None)
    if title is not None:
        out["title_score"] = round(title, 4)
    context = getattr(result, "context", None)
    if context is not None:
        out["context"] = context
    # Issue #4544: attribute the per-prefix tier weight applied to the score
    # (omitted when unboosted to keep responses compact).
    tier_boost = getattr(result, "tier_boost", None)
    if tier_boost is not None:
        out["tier_boost"] = round(tier_boost, 4)
    # #3778 marker, stamped by the daemon when the dense leg was unavailable
    # for a semantic-weighted fusion request (#4541 review round 6). Emitted
    # only when set so default responses stay byte-identical.
    if getattr(result, "semantic_degraded", None):
        out["semantic_degraded"] = True
    macro_text = getattr(result, "macro_text", None)
    if macro_text is not None:
        out["macro_text"] = macro_text
        out["macro_line_start"] = getattr(result, "macro_line_start", None)
        out["macro_line_end"] = getattr(result, "macro_line_end", None)
    # Issue #4543: attribute the recency multiplier applied to the score
    # (omitted when the boost did not fire to keep responses compact).
    recency_boost = getattr(result, "recency_boost", None)
    if recency_boost is not None:
        out["recency_boost"] = round(recency_boost, 4)
    return out
