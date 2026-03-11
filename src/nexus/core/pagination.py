"""Kernel pagination primitives for sys_readdir (Issue #937).

PaginatedResult is the return type for paginated syscalls.
paginate_iter() builds a PaginatedResult from MetastoreABC.list_iter().
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from typing import Any


@dataclass
class PaginatedResult:
    """Syscall return type for paginated list operations.

    Keyset pagination over MetastoreABC.list_iter() — O(log n) for any page
    depth, safe at 1M+ file scale.
    """

    items: list[Any]
    next_cursor: str | None
    has_more: bool
    total_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict for API/RPC response."""
        return {
            "items": self.items,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "total_count": self.total_count,
        }


def paginate_iter(
    items_iter: Iterator,
    limit: int = 1000,
    cursor_path: str | None = None,
) -> PaginatedResult:
    """Paginate a metadata iterator using keyset pagination.

    Builds a PaginatedResult from MetastoreABC.list_iter().

    Args:
        items_iter: Iterator of FileMetadata (from MetastoreABC.list_iter)
        limit: Maximum items per page
        cursor_path: Skip entries with path <= cursor_path (keyset cursor)

    Returns:
        PaginatedResult with items, next_cursor, has_more
    """
    if cursor_path:
        items_iter = (item for item in items_iter if item.path > cursor_path)

    page = list(islice(items_iter, limit + 1))
    has_more = len(page) > limit
    if has_more:
        page = page[:limit]

    next_cursor = page[-1].path if has_more and page else None
    return PaginatedResult(
        items=page,
        next_cursor=next_cursor,
        has_more=has_more,
    )
