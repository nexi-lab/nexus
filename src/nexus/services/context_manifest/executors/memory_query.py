"""MemoryQueryExecutor — resolve memory_query sources via semantic search (Issue #1428).

Executes a semantic/keyword/hybrid search over agent memory and returns
the top-k results with metadata. Template variables in the query string
are resolved before searching.

Performance:
    - Blocking search runs in thread pool to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from nexus.services.context_manifest.executors.memory_search_adapter import MemorySearch
from nexus.services.context_manifest.models import ContextSourceProtocol, SourceResult
from nexus.services.context_manifest.template import resolve_template

logger = logging.getLogger(__name__)


class MemoryQueryExecutor:
    """Execute memory_query sources by searching agent memory.

    Args:
        memory_search: A MemorySearch protocol implementation for querying
            the agent's memory store.
    """

    def __init__(self, memory_search: MemorySearch) -> None:
        self._memory_search = memory_search

    async def execute(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Resolve a memory_query source by searching agent memory.

        Delegates to thread pool to avoid blocking the event loop with
        synchronous memory search I/O.

        Args:
            source: A MemoryQuerySource instance (accessed via protocol).
            variables: Template variables for query substitution.

        Returns:
            SourceResult with search results, or error on failure.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._execute_sync, source, variables)

    def _execute_sync(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Synchronous implementation of memory query resolution."""
        start = time.monotonic()
        source_type = source.type
        source_name = source.source_name

        # Extract query and top_k from source
        query: str = getattr(source, "query", "")
        top_k: int = getattr(source, "top_k", 10)

        # Resolve template variables in query
        if "{{" in query:
            try:
                query = resolve_template(query, variables)
            except ValueError as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                return SourceResult.error(
                    source_type=source_type,
                    source_name=source_name,
                    error_message=f"Template resolution failed: {exc}",
                    elapsed_ms=elapsed_ms,
                )

        # Execute search
        try:
            results, actual_search_mode = self._memory_search.search(
                query=query,
                top_k=top_k,
                search_mode="hybrid",
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.warning("Memory search failed for query %r: %s", query, exc)
            return SourceResult.error(
                source_type=source_type,
                source_name=source_name,
                error_message=f"Memory search failed: {exc}",
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - start) * 1000

        data: dict[str, Any] = {
            "results": results,
            "total": len(results),
            "query": query,
            "search_mode": actual_search_mode,
            "top_k": top_k,
        }

        return SourceResult.ok(
            source_type=source_type,
            source_name=source_name,
            data=data,
            elapsed_ms=elapsed_ms,
        )
