"""Batch search request parsing (`POST /query/batch`).

Extracted from ``routers/search.py`` the same way ``_search_serialize.py``
was (the router sits near the repo's 2000-line file cap). This module owns
the pure per-query spec contract; the route keeps auth, the read gate, and
daemon wiring.

Each spec mirrors the single ``/query`` route's public parameter names and
keeps the legacy batch aliases (``query``/``search_type``/``path_filter``).
The public name wins when both are present. Numeric bounds mirror the
single route's FastAPI ``Query()`` validation exactly; the enum params
(``type``/``fusion``/``expand``/``recency``) are validated against the
same accepted sets the single route 400s on, surfaced here as per-query
errors (validation runs AFTER alias resolution, and messages use the
canonical public name — e.g. an invalid ``search_type`` reports ``type``).
Invalid specs return an error MESSAGE rather than raising: the route maps
them to per-entry ``error`` fields so one bad query cannot fail a whole
batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Accepted enum values, mirroring the single /query route's 400 validation.
_TYPE_CHOICES = ("keyword", "semantic", "hybrid")
_FUSION_CHOICES = ("rrf", "weighted", "rrf_weighted")
_EXPAND_CHOICES = ("none", "macro")
_RECENCY_CHOICES = ("off", "on", "auto")


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
    """Best-effort query-text echo for error entries.

    Public name wins by KEY PRESENCE (an explicit ``"q": ""`` must not fall
    through to the legacy alias), and non-string values echo as ``""``
    rather than their Python ``str()`` representation.
    """
    if not isinstance(raw, dict):
        return ""
    value = raw["q"] if "q" in raw else raw.get("query")
    return value if isinstance(value, str) else ""


def _pick(raw: dict[str, Any], public: str, legacy: str) -> Any:
    """Resolve a public-name/legacy-alias pair; public wins when present."""
    if public in raw:
        return raw[public]
    return raw.get(legacy)


def _as_int(value: Any, name: str, lo: int, hi: int) -> int | str:
    try:
        # bool is an int subclass; reject it explicitly so `limit: true`
        # doesn't silently parse as 1. Fractional floats must not silently
        # truncate (`limit: 1.9` -> 1) — the single route rejects them;
        # integral floats (`limit: 5.0`) stay accepted for JSON parity.
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        out = int(value)
    except (TypeError, ValueError):
        return f"{name} must be an integer"
    if not lo <= out <= hi:
        return f"{name} must be between {lo} and {hi}"
    return out


def _check_choice(value: str, name: str, allowed: tuple[str, ...]) -> str | None:
    """Return an error message when ``value`` is not an accepted enum value."""
    if value not in allowed:
        choices = ", ".join(f"'{a}'" for a in allowed)
        return f"{name} must be one of {choices}"
    return None


def _enum_value(
    raw: dict[str, Any],
    name: str,
    allowed: tuple[str, ...],
    default: str,
    *,
    legacy: str | None = None,
) -> tuple[str, str | None]:
    """Resolve one enum param to ``(value, error)``.

    The default applies ONLY when neither the public key nor its legacy
    alias is present. A present-but-non-string value (including explicit
    null) and a present-but-unknown string both error — ``"type": ""``
    must not silently run a hybrid search.
    """
    if name in raw:
        value = raw[name]
    elif legacy is not None and legacy in raw:
        value = raw[legacy]
    else:
        return default, None
    if not isinstance(value, str):
        return default, f"{name} must be a string"
    return value, _check_choice(value, name, allowed)


def _as_float(
    value: Any, name: str, lo: float, hi: float, *, exclusive_lo: bool = False
) -> float | str:
    try:
        if isinstance(value, bool):
            raise ValueError
        out = float(value)
    except (TypeError, ValueError):
        return f"{name} must be a number"
    if exclusive_lo:
        if not (out > lo and out <= hi):
            return f"{name} must be greater than {lo} and at most {hi}"
    else:
        if not (lo <= out <= hi):
            return f"{name} must be between {lo} and {hi}"
    return out


def parse_batch_query_spec(raw: Any) -> ParsedBatchSpec | str:
    """Parse one raw batch query spec.

    Returns a :class:`ParsedBatchSpec` on success or an error message on
    invalid input. Bounds mirror the single ``/query`` route: ``limit``
    1..100, ``alpha`` 0.0..1.0, ``rrf_k`` 1..1000, ``recency_weight``
    0.0..5.0, ``recency_half_life_days`` >0.0..3650.0. Enum params mirror
    it too: ``type`` in {keyword, semantic, hybrid}, ``fusion`` in
    {rrf, weighted, rrf_weighted}, ``expand`` in {none, macro}, ``recency``
    (when provided) in {off, on, auto}.
    """
    if not isinstance(raw, dict):
        return "invalid query spec: expected an object"

    if "q" in raw or "query" in raw:
        raw_query = raw["q"] if "q" in raw else raw["query"]
    else:
        return "query text required (q)"
    if not isinstance(raw_query, str) or not raw_query:
        return "query text must be a non-empty string (q)"
    query = raw_query

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

    # Enum validation runs after alias resolution; messages use the
    # canonical public name (so `search_type: "keywrod"` errors on "type").
    # Defaults apply ONLY when the key (and its alias) is absent — an
    # explicit empty string or null must error like the single route does,
    # not silently coerce to the default semantics.
    search_type, err = _enum_value(raw, "type", _TYPE_CHOICES, "hybrid", legacy="search_type")
    if err is not None:
        return err
    fusion_method, err = _enum_value(raw, "fusion", _FUSION_CHOICES, "rrf", legacy="fusion_method")
    if err is not None:
        return err
    expand, err = _enum_value(raw, "expand", _EXPAND_CHOICES, "none")
    if err is not None:
        return err

    # recency is the only optional enum: absent AND explicit null both mean
    # "no recency directive"; any other non-string or unknown value errors.
    recency: str | None = None
    if raw.get("recency") is not None:
        raw_recency = raw["recency"]
        if not isinstance(raw_recency, str):
            return "recency must be a string"
        if (err := _check_choice(raw_recency, "recency", _RECENCY_CHOICES)) is not None:
            return err
        recency = raw_recency

    return ParsedBatchSpec(
        query=query,
        search_type=search_type,
        limit=limit,
        path_filter=path,
        alpha=alpha,
        fusion_method=fusion_method,
        rrf_k=rrf_k,
        expand=expand,
        recency=recency,
        recency_weight=recency_weight,
        recency_half_life_days=recency_half_life,
    )
