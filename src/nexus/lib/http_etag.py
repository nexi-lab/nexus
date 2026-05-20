"""Shared RFC 9110 ETag-header → OCC param translation (Issue #4133).

Both the deprecated ``POST /api/nfs/{method}`` route (in
``nexus.server.api.core.rpc``) and the typed ``POST /api/v2/files/write``
route (in ``nexus.server.api.v2.routers.async_files``) need to translate
``If-Match`` / ``If-None-Match`` headers into the keyword arguments
``nexus.lib.occ.occ_write_sync`` understands. Duplicating that translation
in two places is a foot-gun — round 8's adversarial review caught a real
weak-only-If-Match bypass on the v2 route that the deprecated route had
already fixed in round 7. This module is the single canonical
implementation.
"""

from __future__ import annotations

from typing import TypedDict


class OccPreconditions(TypedDict, total=False):
    """OCC kwargs derivable from HTTP If-Match / If-None-Match headers."""

    if_match_star: bool  # ``If-Match: *`` (proceed iff resource exists)
    if_match_any: list[str]  # ``If-Match: "a", "b"`` (any-match)
    if_none_match: bool  # ``If-None-Match: *`` (create-only)
    if_none_match_any: list[str]  # ``If-None-Match: "a", "b"`` (none-match)


# Sentinel content_id used to make weak-only If-Match a guaranteed
# conflict. RFC 9110 §13.1.1 forbids weak validators on state-changing
# preconditions; rather than treating "weak-only" as "no precondition"
# (which would silently let the write through), inject this opaque tag
# into ``if_match_any`` so ``occ_write`` raises ``ConflictError``.
WEAK_ONLY_IF_MATCH_SENTINEL = "__nx_weak_only_if_match__"


def _parse_etag_list(raw: str | None, *, strong_only: bool) -> list[str]:
    """Parse an entity-tag list per RFC 9110 §8.8.3.

    Each element is ``("W/")? '"' opaque-tag '"'``. When ``strong_only``
    is True (required by ``If-Match`` for state-changing requests — RFC
    9110 §13.1.1), weak validators (``W/`` prefix) are dropped instead
    of silently downgraded to strong.
    """
    if not raw:
        return []
    tags: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        is_weak = p.startswith("W/")
        if is_weak:
            p = p[2:].strip()
        p = p.strip('"')
        if not p:
            continue
        if is_weak and strong_only:
            continue
        tags.append(p)
    return tags


def parse_write_preconditions(
    if_match_header: str | None,
    if_none_match_header: str | None,
) -> OccPreconditions:
    """Translate HTTP ``If-Match`` / ``If-None-Match`` headers into the
    kwargs ``nexus.lib.occ.occ_write_sync`` understands.

    Returns a dict suitable for ``**``-spreading into ``occ_write`` /
    ``occ_write_sync``. Keys omitted entirely when the header isn't
    present — callers can ``{**existing_kwargs, **parsed}`` without
    overriding body-level OCC fields.

    Semantics (RFC 9110 §13.1):

      * ``If-Match: *``          → ``if_match_star=True``
      * ``If-Match: "a", "b"``   → ``if_match_any=[a, b]`` (strong-only;
        weak validators dropped per §13.1.1)
      * Weak-only ``If-Match: W/"x"`` → ``if_match_any=[sentinel]`` so
        the write fails with ``ConflictError`` instead of falling
        through unconditionally. (Treating weak-only as "no
        precondition" is the bug round 7+8 caught on each surface.)
      * ``If-None-Match: *``     → ``if_none_match=True`` (create-only)
      * ``If-None-Match: "a"``   → ``if_none_match_any=[a]``
    """
    out: OccPreconditions = {}

    # ---- If-Match ---------------------------------------------------------
    if if_match_header is not None:
        raw = if_match_header.strip()
        if raw == "*":
            out["if_match_star"] = True
        else:
            tags = _parse_etag_list(if_match_header, strong_only=True)
            if tags:
                out["if_match_any"] = tags
            else:
                # Header was present but parsed empty (weak-only or
                # otherwise malformed) — DO NOT fall through to plain
                # write. Inject an unsatisfiable tag to surface the
                # precondition failure.
                out["if_match_any"] = [WEAK_ONLY_IF_MATCH_SENTINEL]

    # ---- If-None-Match ----------------------------------------------------
    if if_none_match_header is not None:
        raw = if_none_match_header.strip()
        if raw == "*":
            out["if_none_match"] = True
        else:
            tags = _parse_etag_list(if_none_match_header, strong_only=False)
            if tags:
                out["if_none_match_any"] = tags
            # Malformed / empty If-None-Match: per RFC 9110 §13.1.2 a
            # syntactically valid but empty list shouldn't change the
            # request. Don't inject anything; the request proceeds as
            # if the header were absent.

    return out
