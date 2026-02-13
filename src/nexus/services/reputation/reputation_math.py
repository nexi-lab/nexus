"""Bayesian Beta reputation math (Issue #1356, Decision #2A).

Pure functions for multi-dimensional reputation scoring. No ORM dependency.

Beta distribution scoring:
- Each dimension (reliability, quality, timeliness, fairness) is modeled as
  Beta(alpha, beta) where alpha = 1 + positive, beta = 1 + negative.
- Prior: Beta(1, 1) = uniform distribution (no information).
- Score = alpha / (alpha + beta) = E[Beta(alpha, beta)].
- Confidence = 1 - variance_ratio, scaled by observation count.

Time decay:
- Exponential decay with configurable half-life (default 30 days).
- Recent events weigh more than older ones.
"""

from __future__ import annotations

import math

# Default half-life: 30 days in seconds
DEFAULT_HALF_LIFE_SECONDS: float = 30 * 24 * 3600  # 2_592_000

# Dimension weights for composite score (must sum to 1.0)
DEFAULT_DIMENSION_WEIGHTS: dict[str, float] = {
    "reliability": 0.30,
    "quality": 0.30,
    "timeliness": 0.20,
    "fairness": 0.20,
}


def compute_beta_score(alpha: float, beta: float) -> float:
    """Compute expected value of Beta(alpha, beta).

    Returns alpha / (alpha + beta). For the prior Beta(1, 1) this gives 0.5.

    Args:
        alpha: Positive evidence count + 1 (prior).
        beta: Negative evidence count + 1 (prior).

    Returns:
        Score in [0, 1].
    """
    total = alpha + beta
    if total <= 0:
        return 0.5
    return alpha / total


def compute_confidence(alpha: float, beta: float) -> float:
    """Compute confidence level from Beta distribution parameters.

    Confidence is based on total observations (alpha + beta - 2, since prior is Beta(1,1)).
    Uses a logarithmic scale: confidence = 1 - 1 / (1 + ln(1 + n))
    where n = alpha + beta - 2 (number of actual observations).

    This gives:
    - 0 observations → confidence = 0.0
    - 1 observation  → confidence ≈ 0.41
    - 10 observations → confidence ≈ 0.71
    - 100 observations → confidence ≈ 0.82
    - 1000 observations → confidence ≈ 0.87

    Args:
        alpha: Positive evidence count + 1.
        beta: Negative evidence count + 1.

    Returns:
        Confidence in [0, 1].
    """
    n = alpha + beta - 2.0  # Subtract prior
    if n <= 0:
        return 0.0
    return 1.0 - 1.0 / (1.0 + math.log(1.0 + n))


def compute_decay_weight(
    age_seconds: float,
    half_life_seconds: float = DEFAULT_HALF_LIFE_SECONDS,
) -> float:
    """Compute exponential time-decay weight.

    weight = 2^(-age / half_life) = exp(-age * ln(2) / half_life).

    Args:
        age_seconds: Age of the event in seconds (non-negative).
        half_life_seconds: Half-life in seconds (default 30 days).

    Returns:
        Weight in (0, 1]. Returns 1.0 for age=0, 0.5 for age=half_life.
    """
    if age_seconds <= 0:
        return 1.0
    if half_life_seconds <= 0:
        return 0.0
    return math.pow(2.0, -age_seconds / half_life_seconds)


def compute_composite_score(
    dimensions: dict[str, tuple[float, float]],
    weights: dict[str, float] | None = None,
) -> tuple[float, float]:
    """Compute weighted composite score and confidence from per-dimension Beta params.

    Args:
        dimensions: Mapping of dimension name → (alpha, beta).
            Example: {"reliability": (11.0, 2.0), "quality": (8.0, 3.0), ...}
        weights: Optional dimension weights. Defaults to DEFAULT_DIMENSION_WEIGHTS.
            Weights for dimensions present in `dimensions` are normalized to sum to 1.0.

    Returns:
        Tuple of (composite_score, composite_confidence) both in [0, 1].
    """
    if not dimensions:
        return 0.5, 0.0

    if weights is None:
        weights = DEFAULT_DIMENSION_WEIGHTS

    # Only consider dimensions that are present in both dicts
    active_dims = [d for d in dimensions if d in weights]
    if not active_dims:
        # Fall back: equal weights for all provided dimensions
        active_dims = list(dimensions.keys())
        w = 1.0 / len(active_dims) if active_dims else 0.0
        effective_weights = dict.fromkeys(active_dims, w)
    else:
        # Normalize weights for active dimensions
        total_weight = sum(weights[d] for d in active_dims)
        if total_weight <= 0:
            return 0.5, 0.0
        effective_weights = {d: weights[d] / total_weight for d in active_dims}

    composite_score = 0.0
    composite_confidence = 0.0
    for dim in active_dims:
        alpha, beta = dimensions[dim]
        score = compute_beta_score(alpha, beta)
        conf = compute_confidence(alpha, beta)
        w = effective_weights[dim]
        composite_score += w * score
        composite_confidence += w * conf

    return composite_score, composite_confidence
