"""Memory search adapter for MemoryQueryExecutor (Issue #1428).

Wraps the Memory.search() API behind a thin Protocol so the executor
can be tested with stubs and swapped for different backends.

The adapter detects search-mode fallback (hybrid → keyword when no
embeddings are available) and exposes the actual mode used.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MemorySearch(Protocol):
    """Protocol for memory search backends."""

    def search(self, query: str, top_k: int, search_mode: str) -> tuple[list[dict[str, Any]], str]:
        """Search memory and return (results, actual_search_mode)."""
        ...


class MemorySearchAdapter:
    """Adapts the Memory.search() API to the MemorySearch protocol.

    Catches fallback from hybrid → keyword when no embedding provider
    is available, and reports the actual search mode used.
    """

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    def search(
        self, query: str, top_k: int, search_mode: str = "hybrid"
    ) -> tuple[list[dict[str, Any]], str]:
        """Search memory, returning (results, actual_search_mode).

        If hybrid/semantic mode falls back to keyword internally,
        the returned mode will be "keyword".
        """
        results = self._memory.search(query=query, limit=top_k, search_mode=search_mode)

        # Detect fallback: if we requested hybrid but got results without
        # embedding scores, the backend fell back to keyword.
        actual_mode = search_mode
        if search_mode in ("hybrid", "semantic") and results:
            # Memory.search() keyword results have score=1.0 (text-match)
            # while semantic/hybrid results have variable scores.
            # A heuristic: if all scores are exactly 1.0, it was keyword.
            all_exact_one = all(r.get("score") == 1.0 for r in results)
            if all_exact_one:
                actual_mode = "keyword"

        return results, actual_mode
