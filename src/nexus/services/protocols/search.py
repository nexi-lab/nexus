"""Search service protocol (Issue #1287: Extract domain services).

Defines the contract for file listing, glob, grep, and semantic search.
Existing implementation: ``nexus.services.search_service.SearchService``.

Adaptive algorithm selection (Issue #929):
- Grep: sequential → parallel → Zoekt based on file count
- Glob: Python → Rust acceleration based on file count

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
    - Issue #929: Adaptive algorithm selection
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from nexus.core.permissions import OperationContext


@runtime_checkable
class SearchProtocol(Protocol):
    """Service contract for search operations.

    Four tiers:
    - ``list``: Directory listing with pagination (sync)
    - ``glob`` / ``glob_batch``: Pattern matching (sync)
    - ``grep``: Content search with adaptive strategy selection (sync)
    - ``semantic_search``: Natural language queries over indexed documents (async)
    """

    # ── Sync operations ─────────────────────────────────────────────────

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
        context: OperationContext | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[str] | list[dict[str, Any]] | Any: ...

    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: OperationContext | None = None,
    ) -> list[str]: ...

    def glob_batch(
        self,
        patterns: list[str],
        path: str = "/",
        context: OperationContext | None = None,
    ) -> dict[str, list[str]]: ...

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]: ...

    # ── Async operations (semantic search requires I/O) ─────────────────

    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
        adaptive_k: bool = False,
    ) -> list[dict[str, Any]]: ...

    async def semantic_search_index(
        self,
        path: str = "/",
        recursive: bool = True,
    ) -> dict[str, int]: ...

    async def semantic_search_stats(self) -> dict[str, Any]: ...
