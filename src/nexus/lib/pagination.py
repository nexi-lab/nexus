"""Cursor encoding/decoding and response envelope helpers for brick pagination.

Issue #937: tamper-resistant, URL-safe cursor tokens.
Issue #3701: shared offset/limit envelope builder for grep/glob/list
responses so transports (MCP, HTTP) emit the same shape.

Kernel pagination primitives (PaginatedResult, paginate_iter) live in
``nexus.core.pagination``.

This module must stay cross-brick safe (zero imports from
``nexus.bricks.*``) because MCP, search, and HTTP routers all depend
on it.
"""

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Offset/limit response envelope (#3701)
# =============================================================================


def build_paginated_list_response(
    *,
    items: list[Any],
    total: int,
    offset: int,
    limit: int,
    extras: dict[str, Any] | None = None,
    has_more: bool | None = None,
) -> dict[str, Any]:
    """Build a canonical paginated list envelope.

    Used by grep/glob/list transports that return a list of items with
    classic offset/limit pagination. Prior to #3701 this shape was
    duplicated across MCP ``nexus_grep``/``nexus_glob`` and would have
    been re-duplicated again when the HTTP grep/glob endpoints landed.

    Args:
        items: The slice to return (already paginated — this helper
            does NOT paginate for you).
        total: Total number of items in the full result set, *before*
            pagination was applied. May be a lower bound when the
            caller fetched via a sentinel approach (see ``has_more``).
        offset: The offset at which ``items`` starts in the full set.
        limit: The limit that was requested for this page.
        extras: Optional extra fields to merge into the envelope. Used
            to carry transport-specific additions such as ``stale_count``
            or ``truncated_by_permissions``. Collisions with core keys
            are deliberate escape hatches — extras win.
        has_more: Explicit override for the ``has_more`` field. Use
            this when ``total`` is a lower bound (sentinel-style fetch)
            rather than the true total, so the caller can set
            ``has_more`` independently. Defaults to ``offset + limit <
            total`` when not provided. Flagged by Codex adversarial
            review of #3701: fetching ``limit + offset`` and treating
            the length as the true total silently reports
            ``has_more=False`` on the first page of a large result set
            if the SearchService cap happens to match.

    Returns:
        ``{total, count, offset, items, has_more, next_offset, ...}``

    Conventions:
        * Envelopes are **additive-only**: new fields must be optional so
          existing clients tolerating unknown keys continue to work.
        * ``count`` is ``len(items)``, which may differ from ``limit`` on
          the final page.
        * ``has_more`` is ``True`` iff there are items after the current
          page. When the caller can detect this independently (e.g. via
          a sentinel fetch of ``limit + 1``), pass it explicitly.
          Otherwise it defaults to ``offset + limit < total``.
    """
    if has_more is None:
        has_more = (offset + limit) < total
    envelope: dict[str, Any] = {
        "total": total,
        "count": len(items),
        "offset": offset,
        "items": items,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }
    if extras:
        envelope.update(extras)
    return envelope


# =============================================================================
# Cursor encoding (#937)
# =============================================================================


class CursorError(Exception):
    """Raised when cursor is invalid, expired, or tampered."""

    pass


@dataclass
class CursorData:
    """Decoded cursor containing pagination state."""

    path: str  # Last item's virtual_path (primary sort key)
    path_id: str | None  # Last item's path_id (tiebreaker for duplicates)
    filters_hash: str  # Hash of query filters to detect tampering


def encode_cursor(
    last_path: str,
    last_path_id: str | None,
    filters: dict[str, Any],
) -> str:
    """Encode pagination cursor as URL-safe base64 string.

    Cursor format: Base64(JSON({p: path, i: id, h: filters_hash}))

    The cursor encodes the last item's position to enable keyset pagination.
    A filters hash is included to detect if query parameters changed between
    pages (which would invalidate the cursor).

    Args:
        last_path: Virtual path of last item in current page
        last_path_id: UUID of last item (tiebreaker for stable ordering)
        filters: Query filters (prefix, recursive, zone_id) for tampering detection

    Returns:
        URL-safe base64-encoded cursor string
    """
    filters_hash = _hash_filters(filters)
    data = {
        "p": last_path,
        "i": last_path_id,
        "h": filters_hash,
    }
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(json_bytes).decode("ascii")


def decode_cursor(cursor: str, filters: dict[str, Any]) -> CursorData:
    """Decode and validate pagination cursor.

    Validates that:
    1. Cursor is valid base64-encoded JSON
    2. Required fields (path, hash) are present
    3. Filter hash matches current query parameters

    Args:
        cursor: Base64-encoded cursor string
        filters: Current query filters (must match cursor's filters)

    Returns:
        CursorData with decoded pagination state

    Raises:
        CursorError: If cursor is malformed, expired, or filters changed
    """
    try:
        json_bytes = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(json_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        logger.debug(f"Failed to decode cursor: {e}")
        raise CursorError(f"Malformed cursor: {e}") from e

    if not isinstance(data, dict):
        raise CursorError("Malformed cursor: expected JSON object")

    if "p" not in data or "h" not in data:
        raise CursorError("Cursor missing required fields")

    expected_hash = _hash_filters(filters)
    if data["h"] != expected_hash:
        raise CursorError(
            "Cursor filters mismatch - query parameters changed between pages. "
            "Start pagination from the beginning with a new request."
        )

    return CursorData(
        path=data["p"],
        path_id=data.get("i"),
        filters_hash=data["h"],
    )


def _hash_filters(filters: dict[str, Any]) -> str:
    """Create deterministic hash of filter parameters."""
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
