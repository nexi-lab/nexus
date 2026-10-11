"""Limits applied to federated search results."""

from typing import Any


def cap_chunks_per_page(
    chunks: list[Any],
    *,
    chunks_per_page: int,
) -> list[Any]:
    """Keep up to the requested number of chunks per path in input order.

    Accept result dictionaries or objects. Rows without a path each count
    as their own page.
    """
    counts: dict[str, int] = {}
    out: list[Any] = []
    for r in chunks:
        if isinstance(r, dict):
            key = (r.get("path") or "") or str(r.get("id") or id(r))
        else:
            key = getattr(r, "path", "") or str(id(r))
        emitted = counts.get(key, 0)
        if emitted < chunks_per_page:
            counts[key] = emitted + 1
            out.append(r)
    return out
