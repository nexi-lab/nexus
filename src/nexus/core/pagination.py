"""Cursor-based pagination utilities for Nexus APIs (Issue #937).

This module provides utilities for implementing cursor-based (keyset) pagination,
enabling efficient traversal of large datasets at 1M+ file scale.

Key features:
- O(log n) performance for any page depth (vs O(n) for OFFSET)
- Tamper-resistant cursors with filter hash validation
- URL-safe Base64 encoding for cursor tokens
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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

    Example:
        >>> cursor = encode_cursor("/files/doc999.txt", "uuid-123", {"prefix": "/files/"})
        >>> # Returns: "eyJwIjoiL2ZpbGVzL2RvYzk5OS50eHQiLCJpIjoidXVpZC0xMjMiLCJoIjoiYWJjMTIzIn0="
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

    Example:
        >>> data = decode_cursor(cursor, {"prefix": "/files/"})
        >>> print(data.path)  # "/files/doc999.txt"
    """
    try:
        # Decode base64
        json_bytes = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(json_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        logger.debug(f"Failed to decode cursor: {e}")
        raise CursorError(f"Malformed cursor: {e}") from e

    # Validate required fields
    if "p" not in data or "h" not in data:
        raise CursorError("Cursor missing required fields")

    # Validate filters hash to detect tampering or query parameter changes
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
    """Create deterministic hash of filter parameters.

    Used to detect if client changed query parameters between pages,
    which would invalidate the cursor position.

    Args:
        filters: Dictionary of filter parameters

    Returns:
        16-character hex hash (truncated SHA-256)
    """
    # Sort keys for deterministic ordering
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
