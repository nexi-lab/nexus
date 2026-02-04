"""Fusion algorithms for hybrid search.

Provides multiple fusion methods for combining keyword (BM25) and vector search results:
- RRF (Reciprocal Rank Fusion): Rank-based, no score normalization needed
- Weighted: Score-based with optional min-max normalization
- RRF Weighted: RRF with alpha weighting for BM25/vector bias

Reference:
    - RRF Paper: https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf
    - Weaviate Hybrid Search: https://weaviate.io/blog/hybrid-search-explained

Issue: #798
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FusionMethod(StrEnum):
    """Fusion method for combining keyword and vector search results."""

    RRF = "rrf"  # Reciprocal Rank Fusion (default, recommended)
    WEIGHTED = "weighted"  # Simple weighted linear combination
    RRF_WEIGHTED = "rrf_weighted"  # RRF with alpha weighting


@dataclass
class FusionConfig:
    """Configuration for fusion algorithms.

    Attributes:
        method: Fusion algorithm to use
        alpha: Weight for vector search (0.0 = all BM25, 1.0 = all vector)
        rrf_k: RRF constant (default: 60 per original paper)
        normalize_scores: Apply min-max normalization for weighted fusion
    """

    method: FusionMethod = FusionMethod.RRF
    alpha: float = 0.5
    rrf_k: int = 60
    normalize_scores: bool = True


def normalize_scores_minmax(scores: list[float]) -> list[float]:
    """Apply min-max normalization to scores.

    Scales scores to [0, 1] range while preserving relative ordering.

    Args:
        scores: Raw scores (can be any range)

    Returns:
        Normalized scores in [0, 1] range
    """
    if not scores:
        return []

    min_score = min(scores)
    max_score = max(scores)

    if max_score == min_score:
        return [1.0] * len(scores)

    return [(s - min_score) / (max_score - min_score) for s in scores]


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


def rrf_fusion(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    k: int = 60,
    limit: int = 10,
    id_key: str | None = "chunk_id",
) -> list[dict[str, Any]]:
    """Combine results using Reciprocal Rank Fusion.

    RRF score = sum(1 / (k + rank)) for each result list

    RRF is robust because it:
    - Doesn't require score normalization
    - Works well when scoring scales differ between search methods
    - Is stable across different query types

    Args:
        keyword_results: Results from keyword search (ranked by BM25)
        vector_results: Results from vector search (ranked by similarity)
        k: RRF constant (default: 60, per original paper)
        limit: Maximum results to return
        id_key: Key for identifying unique results, or None for path:chunk_index

    Returns:
        Combined results ranked by RRF score
    """
    rrf_scores: dict[str, dict[str, Any]] = {}

    # Add keyword results
    for rank, result in enumerate(keyword_results, start=1):
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += 1.0 / (k + rank)
        rrf_scores[key]["result"]["keyword_score"] = result.get("score", 0.0)

    # Add vector results
    for rank, result in enumerate(vector_results, start=1):
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += 1.0 / (k + rank)
        rrf_scores[key]["result"]["vector_score"] = result.get("score", 0.0)

    # Sort by RRF score
    sorted_results = sorted(
        rrf_scores.values(),
        key=lambda x: x["rrf_score"],
        reverse=True,
    )[:limit]

    # Update final scores
    for item in sorted_results:
        item["result"]["score"] = item["rrf_score"]

    return [item["result"] for item in sorted_results]


def weighted_fusion(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    alpha: float = 0.5,
    normalize: bool = True,
    limit: int = 10,
    id_key: str | None = "chunk_id",
) -> list[dict[str, Any]]:
    """Combine results using weighted linear combination.

    Final score = (1 - alpha) * keyword_score + alpha * vector_score

    Args:
        keyword_results: Results from keyword search
        vector_results: Results from vector search
        alpha: Weight for vector search (0.0 = all BM25, 1.0 = all vector)
        normalize: Apply min-max normalization before combining
        limit: Maximum results to return
        id_key: Key for identifying unique results

    Returns:
        Combined results ranked by weighted score
    """
    # Create working copies with normalized scores if requested
    keyword_work = []
    if keyword_results:
        keyword_scores = [r.get("score", 0.0) for r in keyword_results]
        normalized_keyword = (
            normalize_scores_minmax(keyword_scores) if normalize else keyword_scores
        )
        for i, r in enumerate(keyword_results):
            work = r.copy()
            work["_norm_score"] = normalized_keyword[i]
            keyword_work.append(work)

    vector_work = []
    if vector_results:
        vector_scores = [r.get("score", 0.0) for r in vector_results]
        normalized_vector = normalize_scores_minmax(vector_scores) if normalize else vector_scores
        for i, r in enumerate(vector_results):
            work = r.copy()
            work["_norm_score"] = normalized_vector[i]
            vector_work.append(work)

    # Merge results
    results_map: dict[str, dict[str, Any]] = {}

    for result in keyword_work:
        key = _get_result_key(result, id_key)
        results_map[key] = result.copy()
        results_map[key]["keyword_score"] = result.get("score", 0.0)
        results_map[key]["_keyword_norm"] = result.get("_norm_score", 0.0)
        results_map[key]["_vector_norm"] = 0.0
        results_map[key].pop("_norm_score", None)

    for result in vector_work:
        key = _get_result_key(result, id_key)
        if key in results_map:
            results_map[key]["vector_score"] = result.get("score", 0.0)
            results_map[key]["_vector_norm"] = result.get("_norm_score", 0.0)
        else:
            results_map[key] = result.copy()
            results_map[key]["keyword_score"] = 0.0
            results_map[key]["vector_score"] = result.get("score", 0.0)
            results_map[key]["_keyword_norm"] = 0.0
            results_map[key]["_vector_norm"] = result.get("_norm_score", 0.0)
            results_map[key].pop("_norm_score", None)

    # Calculate combined scores
    for result in results_map.values():
        result["score"] = (1 - alpha) * result["_keyword_norm"] + alpha * result["_vector_norm"]
        # Clean up internal fields
        result.pop("_keyword_norm", None)
        result.pop("_vector_norm", None)

    # Sort and return
    return sorted(results_map.values(), key=lambda x: x["score"], reverse=True)[:limit]


def rrf_weighted_fusion(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    alpha: float = 0.5,
    k: int = 60,
    limit: int = 10,
    id_key: str | None = "chunk_id",
) -> list[dict[str, Any]]:
    """Combine results using RRF with alpha weighting.

    RRF score = (1 - alpha) * (1/(k+keyword_rank)) + alpha * (1/(k+vector_rank))

    This combines the robustness of RRF (no score normalization needed)
    with the ability to bias towards keyword or vector search.

    Args:
        keyword_results: Results from keyword search
        vector_results: Results from vector search
        alpha: Weight for vector contribution (0.0 = all BM25, 1.0 = all vector)
        k: RRF constant
        limit: Maximum results to return
        id_key: Key for identifying unique results

    Returns:
        Combined results ranked by weighted RRF score
    """
    rrf_scores: dict[str, dict[str, Any]] = {}

    # Add keyword results with (1 - alpha) weight
    for rank, result in enumerate(keyword_results, start=1):
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += (1 - alpha) * (1.0 / (k + rank))
        rrf_scores[key]["result"]["keyword_score"] = result.get("score", 0.0)

    # Add vector results with alpha weight
    for rank, result in enumerate(vector_results, start=1):
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += alpha * (1.0 / (k + rank))
        rrf_scores[key]["result"]["vector_score"] = result.get("score", 0.0)

    # Sort by RRF score
    sorted_results = sorted(
        rrf_scores.values(),
        key=lambda x: x["rrf_score"],
        reverse=True,
    )[:limit]

    # Update final scores
    for item in sorted_results:
        item["result"]["score"] = item["rrf_score"]

    return [item["result"] for item in sorted_results]


def fuse_results(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    config: FusionConfig | None = None,
    limit: int = 10,
    id_key: str | None = "chunk_id",
) -> list[dict[str, Any]]:
    """Fuse keyword and vector search results using configured method.

    This is the main entry point for hybrid search fusion. It dispatches
    to the appropriate fusion algorithm based on the configuration.

    Args:
        keyword_results: Results from keyword/BM25 search
        vector_results: Results from vector/semantic search
        config: Fusion configuration (defaults to RRF with k=60)
        limit: Maximum results to return
        id_key: Key for identifying unique results

    Returns:
        Combined results ranked by fusion score

    Raises:
        ValueError: If an unknown fusion method is specified

    Example:
        >>> config = FusionConfig(method=FusionMethod.RRF, rrf_k=60)
        >>> results = fuse_results(keyword_results, vector_results, config)
    """
    if config is None:
        config = FusionConfig()

    if config.method == FusionMethod.RRF:
        return rrf_fusion(
            keyword_results,
            vector_results,
            k=config.rrf_k,
            limit=limit,
            id_key=id_key,
        )
    elif config.method == FusionMethod.WEIGHTED:
        return weighted_fusion(
            keyword_results,
            vector_results,
            alpha=config.alpha,
            normalize=config.normalize_scores,
            limit=limit,
            id_key=id_key,
        )
    elif config.method == FusionMethod.RRF_WEIGHTED:
        return rrf_weighted_fusion(
            keyword_results,
            vector_results,
            alpha=config.alpha,
            k=config.rrf_k,
            limit=limit,
            id_key=id_key,
        )
    else:
        raise ValueError(f"Unknown fusion method: {config.method}")
