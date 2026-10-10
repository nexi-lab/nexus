"""Merge ranked results from multiple zones with reciprocal rank fusion."""

from collections.abc import Sequence
from typing import Any

# Issue #3773: Top-rank bonus preserves high-confidence matches
# against dilution from query expansion and multi-source fusion.
RRF_TOP1_BONUS = 0.05
RRF_TOP3_BONUS = 0.02


def _to_dict(result: Any) -> dict[str, Any]:
    """Convert a result (dict or dataclass) to dict.

    Uses direct field access for dataclasses (3-5x faster than asdict()).
    Also includes @property values (e.g., zone_qualified_path) so that
    id_key-based dedup works correctly for computed fields.
    """
    if isinstance(result, dict):
        return result
    fields = getattr(result, "__dataclass_fields__", None)
    if fields is not None:
        d = {f: getattr(result, f) for f in fields}
        # Include @property values that are used for dedup (Issue #3147).
        # Scan for properties on the class and add their values.
        for name in dir(type(result)):
            if isinstance(getattr(type(result), name, None), property):
                val = getattr(result, name, None)
                if val is not None and name not in d:
                    d[name] = val
        return d
    return dict(result) if hasattr(result, "__iter__") else {"value": result}


def _get_result_key(result: dict[str, Any], id_key: str | None) -> str:
    """Get unique key for a result.

    Args:
        result: Search result dict
        id_key: Key to use for identification, or None to use path:chunk_index

    Returns:
        Unique string key for the result
    """
    if id_key and id_key in result:
        return str(result[id_key])
    return f"{result.get('path', '')}:{result.get('chunk_index', 0)}"


def rrf_multi_fusion(
    result_lists: Sequence[tuple[str, Sequence[Any]]],
    k: int = 60,
    limit: int = 10,
    id_key: str | None = "chunk_id",
    top_rank_bonus: bool = True,
) -> list[dict[str, Any]]:
    """Fuse ranked zone results and retain each zone's score attribution.

    The optional top-rank bonus uses the best rank across all input lists.

    Args:
        result_lists: List of (source_name, results) tuples.
            source_name is used to set '{source_name}_score' on each result.
        k: RRF constant (default: 60)
        limit: Maximum results to return
        id_key: Key for identifying unique results, or None for path:chunk_index
        top_rank_bonus: Apply top-rank bonus (Issue #3773). Default True.

    Returns:
        Combined results ranked by RRF score
    """
    rrf_scores: dict[str, dict[str, Any]] = {}
    best_rank: dict[str, int] = {}

    for source_name, results in result_lists:
        score_key = f"{source_name}_score"
        for rank, raw_result in enumerate(results, start=1):
            result = _to_dict(raw_result)
            key = _get_result_key(result, id_key)
            if key not in rrf_scores:
                rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
            rrf_scores[key]["rrf_score"] += 1.0 / (k + rank)
            rrf_scores[key]["result"][score_key] = result.get("score", 0.0)
            best_rank[key] = min(best_rank.get(key, rank), rank)

    if top_rank_bonus:
        for key, entry in rrf_scores.items():
            br = best_rank.get(key, 999)
            if br == 1:
                entry["rrf_score"] += RRF_TOP1_BONUS
            elif br <= 3:
                entry["rrf_score"] += RRF_TOP3_BONUS

    sorted_results = sorted(
        rrf_scores.values(),
        key=lambda x: x["rrf_score"],
        reverse=True,
    )[:limit]

    for item in sorted_results:
        item["result"]["score"] = item["rrf_score"]

    return [item["result"] for item in sorted_results]
