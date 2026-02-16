"""MemoryQueryExecutor — resolve memory_query sources via semantic search (Issue #1428).

Executes a semantic/keyword/hybrid search over agent memory and returns
the top-k results with metadata. Template variables in the query string
are resolved before searching.

Performance:
    - Blocking search runs in thread pool to avoid blocking the event loop.
    - Thread pool is configurable via constructor (14B).
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Executor
from typing import Any

from nexus.services.context_manifest.executors.executor_utils import (
    MemoryQuerySourceProtocol,
    resolve_source_template,
)
from nexus.services.context_manifest.executors.memory_search_adapter import MemorySearch
from nexus.services.context_manifest.models import ContextSourceProtocol, SourceResult

logger = logging.getLogger(__name__)


class MemoryQueryExecutor:
    """Execute memory_query sources by searching agent memory.

    Args:
        memory_search: A MemorySearch protocol implementation for querying
            the agent's memory store.
        thread_pool: Optional thread pool for blocking I/O. Defaults to
            the event loop's default executor.
    """

    def __init__(
        self,
        memory_search: MemorySearch,
        thread_pool: Executor | None = None,
    ) -> None:
        self._memory_search = memory_search
        self._thread_pool = thread_pool

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
        return await loop.run_in_executor(self._thread_pool, self._execute_sync, source, variables)

    def _execute_sync(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Synchronous implementation of memory query resolution."""
        start = time.monotonic()

        # Extract query and top_k via typed protocol (6A)
        query: str = (
            source.query
            if isinstance(source, MemoryQuerySourceProtocol)
            else getattr(source, "query", "")
        )
        top_k: int = (
            source.top_k
            if isinstance(source, MemoryQuerySourceProtocol)
            else getattr(source, "top_k", 10)
        )

        # Resolve template variables in query (5A — shared helper)
        query, err = resolve_source_template(query, variables, source, start)
        if err is not None:
            return err

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
                source_type=source.type,
                source_name=source.source_name,
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
            source_type=source.type,
            source_name=source.source_name,
            data=data,
            elapsed_ms=elapsed_ms,
        )
