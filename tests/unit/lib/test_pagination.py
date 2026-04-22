"""Unit tests for nexus.lib.pagination helpers.

Covers:
* ``build_paginated_list_response`` — the offset/limit envelope builder
  shared by grep/glob/list transports (#3701).
* ``encode_cursor`` / ``decode_cursor`` — cursor token round-trip (#937).
"""

from __future__ import annotations

import pytest

from nexus.lib.pagination import (
    CursorError,
    build_paginated_list_response,
    decode_cursor,
    encode_cursor,
)

# ---------------------------------------------------------------------------
# build_paginated_list_response
# ---------------------------------------------------------------------------


class TestBuildPaginatedListResponse:
    def test_first_page_with_more(self) -> None:
        r = build_paginated_list_response(items=["a", "b", "c"], total=10, offset=0, limit=3)
        assert r["total"] == 10
        assert r["count"] == 3
        assert r["offset"] == 0
        assert r["items"] == ["a", "b", "c"]
        assert r["has_more"] is True
        assert r["next_offset"] == 3

    def test_last_page_no_more(self) -> None:
        r = build_paginated_list_response(items=["x", "y"], total=8, offset=6, limit=3)
        assert r["has_more"] is False
        assert r["next_offset"] is None
        assert r["count"] == 2

    def test_exact_page_boundary_no_more(self) -> None:
        """offset+limit == total should report has_more=False."""
        r = build_paginated_list_response(items=["a", "b", "c"], total=3, offset=0, limit=3)
        assert r["has_more"] is False
        assert r["next_offset"] is None

    def test_empty_items(self) -> None:
        r = build_paginated_list_response(items=[], total=0, offset=0, limit=10)
        assert r == {
            "total": 0,
            "count": 0,
            "offset": 0,
            "items": [],
            "has_more": False,
            "next_offset": None,
        }

    def test_offset_beyond_total(self) -> None:
        r = build_paginated_list_response(items=[], total=5, offset=100, limit=10)
        assert r["count"] == 0
        assert r["has_more"] is False
        assert r["next_offset"] is None

    def test_single_item_single_page(self) -> None:
        r = build_paginated_list_response(items=["only"], total=1, offset=0, limit=100)
        assert r["has_more"] is False
        assert r["count"] == 1

    def test_extras_are_merged(self) -> None:
        r = build_paginated_list_response(
            items=["a"],
            total=1,
            offset=0,
            limit=10,
            extras={"stale_count": 2, "truncated_by_permissions": True},
        )
        assert r["stale_count"] == 2
        assert r["truncated_by_permissions"] is True
        # Core fields still present
        assert r["total"] == 1
        assert r["count"] == 1

    def test_extras_can_clobber_core_fields(self) -> None:
        """Extras override core keys — a deliberate escape hatch.

        Callers should NOT rely on this in normal usage. It exists so a
        transport with an unusual need can override the default envelope
        shape. If collisions start happening regularly, one side should
        be renamed.
        """
        r = build_paginated_list_response(
            items=["a"],
            total=1,
            offset=0,
            limit=10,
            extras={"total": 999},
        )
        assert r["total"] == 999

    def test_items_list_is_passed_through_by_reference(self) -> None:
        """The helper does not copy ``items`` — caller owns the list."""
        items = ["a", "b"]
        r = build_paginated_list_response(items=items, total=2, offset=0, limit=2)
        assert r["items"] is items


# ---------------------------------------------------------------------------
# encode_cursor / decode_cursor round-trip (sanity only — existing code)
# ---------------------------------------------------------------------------


class TestCursorRoundTrip:
    def test_round_trip_preserves_path_and_id(self) -> None:
        filters = {"prefix": "/workspace", "recursive": True}
        cursor = encode_cursor("/workspace/a.py", "id-123", filters)
        decoded = decode_cursor(cursor, filters)
        assert decoded.path == "/workspace/a.py"
        assert decoded.path_id == "id-123"

    def test_mismatched_filters_rejected(self) -> None:
        filters = {"prefix": "/workspace", "recursive": True}
        cursor = encode_cursor("/workspace/a.py", "id-123", filters)
        with pytest.raises(CursorError):
            decode_cursor(cursor, {"prefix": "/other", "recursive": True})

    def test_malformed_cursor_rejected(self) -> None:
        with pytest.raises(CursorError):
            decode_cursor("not-a-real-cursor", {})
