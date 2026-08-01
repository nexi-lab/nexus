"""Post-fusion recency boost (Issue #4543).

Pure functions only — DB hydration and mode resolution live on
``SearchDaemon`` (``_apply_recency_boost`` / ``_fetch_recency_mtimes``).

The boost is multiplicative hyperbolic decay::

    score *= 1 + weight * H / (H + age_days)

so a fresh document gets ``×(1 + weight)``, a half-life-old one
``×(1 + weight/2)``, and the multiplier decays toward ``×1`` — it strictly
promotes newer material and can never demote. It is applied AFTER fusion on
final scores; rank positions consumed by RRF and the HNSW pure-distance scan
are never touched (hard rule from the issue).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nexus.contracts.search_types import RECENCY_WORDS

_SECONDS_PER_DAY = 86400.0


def has_recency_intent(query: str) -> bool:
    """True when the query contains a recency-intent word (recency=auto gate)."""
    return bool(set(query.lower().split()) & RECENCY_WORDS)


def apply_recency_boost(
    results: list[Any],
    mtimes: dict[str, datetime],
    *,
    weight: float,
    half_life_days: float,
    now: datetime | None = None,
) -> int:
    """Boost ``results`` in place by mtime recency; return the boosted count.

    Per result with a known mtime and a positive score:
    ``score *= 1 + weight * H / (H + age_days)`` and ``recency_boost`` is set
    to the multiplier (attribution — stays ``None`` when the boost did not
    fire). ``age_days`` is fractional and clamped >= 0 so clock-skewed future
    mtimes get the max boost, never a penalty. Naive datetimes are treated as
    UTC (``file_paths.updated_at`` is a naive-UTC column).

    Results lacking an mtime row (deleted mid-flight, remote-zone results,
    legacy sources) are left untouched. Non-positive scores are skipped —
    a multiplicative boost on a negative score would demote.

    Sorts in place (never rebinds) so ``SearchResultList`` and its
    ``search_timing`` survive, and only when at least one boost fired so a
    no-op call leaves ordering byte-identical.
    """
    if weight <= 0 or half_life_days <= 0 or not results:
        return 0
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    boosted = 0
    for result in results:
        mtime = mtimes.get(result.path)
        if mtime is None or result.score <= 0:
            continue
        if mtime.tzinfo is None:
            mtime = mtime.replace(tzinfo=UTC)
        age_days = max(0.0, (now - mtime).total_seconds() / _SECONDS_PER_DAY)
        boost = 1.0 + weight * half_life_days / (half_life_days + age_days)
        result.score *= boost
        result.recency_boost = boost
        boosted += 1

    if boosted:
        results.sort(key=lambda r: r.score, reverse=True)
    return boosted
