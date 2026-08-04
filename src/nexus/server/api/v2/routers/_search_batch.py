"""Batch search request parsing (`POST /query/batch`).

Extracted from ``routers/search.py`` the same way ``_search_serialize.py``
was (the router sits near the repo's 2000-line file cap). This module owns
the pure per-query spec contract; the route keeps auth, the read gate, and
daemon wiring.

Each spec mirrors the single ``/query`` route's public parameter names and
keeps the legacy batch aliases (``query``/``search_type``/``path_filter``).
The public name wins when both are present. Numeric bounds mirror the
single route's FastAPI ``Query()`` validation exactly; string params pass
through unvalidated (single-route parity — the daemon handles unknown
values). Invalid specs return an error MESSAGE rather than raising: the
route maps them to per-entry ``error`` fields so one bad query cannot fail
a whole batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, kw_only=True)
class ParsedBatchSpec:
    """One validated batch query, normalized to daemon spec key names."""

    query: str
    search_type: str = "hybrid"
    limit: int = 10
    path_filter: str | None = None
    alpha: float = 0.5
    fusion_method: str = "rrf"
    rrf_k: int = 60
    expand: str = "none"
    recency: str | None = None
    recency_weight: float | None = None
    recency_half_life_days: float | None = None


def spec_query_text(raw: Any) -> str:
    """Best-effort query-text echo for error entries (public name wins)."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("q") or raw.get("query") or "")


def _pick(raw: dict[str, Any], public: str, legacy: str) -> Any:
    """Resolve a public-name/legacy-alias pair; public wins when present."""
    if public in raw:
        return raw[public]
    return raw.get(legacy)


def _as_int(value: Any, name: str, lo: int, hi: int) -> int | str:
    try:
        # bool is an int subclass; reject it explicitly so `limit: true`
        # doesn't silently parse as 1.
        if isinstance(value, bool):
            raise ValueError
        out = int(value)
    except (TypeError, ValueError):
        return f"{name} must be an integer"
    if not lo <= out <= hi:
        return f"{name} must be between {lo} and {hi}"
    return out


def _as_float(
    value: Any, name: str, lo: float, hi: float, *, exclusive_lo: bool = False
) -> float | str:
    try:
        if isinstance(value, bool):
            raise ValueError
        out = float(value)
    except (TypeError, ValueError):
        return f"{name} must be a number"
    if exclusive_lo and out <= lo:
        return f"{name} must be greater than {lo} and at most {hi}"
    if not exclusive_lo and not lo <= out <= hi:
        return f"{name} must be between {lo} and {hi}"
    if out > hi:
        return f"{name} must be greater than {lo} and at most {hi}"
    return out


def parse_batch_query_spec(raw: Any) -> ParsedBatchSpec | str:
    """Parse one raw batch query spec.

    Returns a :class:`ParsedBatchSpec` on success or an error message on
    invalid input. Bounds mirror the single ``/query`` route: ``limit``
    1..100, ``alpha`` 0.0..1.0, ``rrf_k`` 1..1000, ``recency_weight``
    0.0..5.0, ``recency_half_life_days`` >0.0..3650.0.
    """
    if not isinstance(raw, dict):
        return "invalid query spec: expected an object"

    query = spec_query_text(raw)
    if not query:
        return "query text required (q)"

    path = _pick(raw, "path", "path_filter")
    if path is not None and not isinstance(path, str):
        return "path must be a string"

    limit = _as_int(raw.get("limit", 10), "limit", 1, 100)
    if isinstance(limit, str):
        return limit
    alpha = _as_float(raw.get("alpha", 0.5), "alpha", 0.0, 1.0)
    if isinstance(alpha, str):
        return alpha
    rrf_k = _as_int(raw.get("rrf_k", 60), "rrf_k", 1, 1000)
    if isinstance(rrf_k, str):
        return rrf_k

    recency_weight: float | None = None
    if raw.get("recency_weight") is not None:
        parsed_weight = _as_float(raw["recency_weight"], "recency_weight", 0.0, 5.0)
        if isinstance(parsed_weight, str):
            return parsed_weight
        recency_weight = parsed_weight

    recency_half_life: float | None = None
    if raw.get("recency_half_life_days") is not None:
        parsed_half_life = _as_float(
            raw["recency_half_life_days"],
            "recency_half_life_days",
            0.0,
            3650.0,
            exclusive_lo=True,
        )
        if isinstance(parsed_half_life, str):
            return parsed_half_life
        recency_half_life = parsed_half_life

    recency = raw.get("recency")

    return ParsedBatchSpec(
        query=query,
        search_type=str(_pick(raw, "type", "search_type") or "hybrid"),
        limit=limit,
        path_filter=path,
        alpha=alpha,
        fusion_method=str(_pick(raw, "fusion", "fusion_method") or "rrf"),
        rrf_k=rrf_k,
        expand=str(raw.get("expand") or "none"),
        recency=str(recency) if recency is not None else None,
        recency_weight=recency_weight,
        recency_half_life_days=recency_half_life,
    )
