"""Search service protocol (Issue #1287: Extract domain services).

Defines the contract for file listing, glob, grep, and semantic search.
Existing implementation: ``nexus.bricks.search.search_service.SearchService``.

Adaptive algorithm selection (Issue #929):
- Grep: sequential → parallel → Zoekt based on file count
- Glob: Python → Rust acceleration based on file count

Issue #1520: Added SearchBrickProtocol for search brick contract.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
    - Issue #929: Adaptive algorithm selection
    - Issue #1520: Extract search module into search brick
"""

import builtins
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

# =============================================================================
# Issue #1520: Search Brick Protocol
# =============================================================================


@runtime_checkable
class SearchBrickProtocol(Protocol):
    """Brick contract for search operations (Issue #1520).

    Defines the interface that search brick implementations must satisfy.
    ``SearchDaemon`` is the canonical implementation.

    Used by the kernel layer to interact with the search service
    without hard-coupling to its internals.
    """

    @property
    def is_initialized(self) -> bool: ...

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def search(
        self,
        query: str,
        search_type: str = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        adaptive_k: bool = False,
        zone_id: str | None = None,
    ) -> builtins.list[Any]: ...

    def get_stats(self) -> dict[str, Any]: ...

    def get_health(self) -> dict[str, Any]: ...

    async def notify_file_change(self, path: str, change_type: str = "update") -> None: ...


# =============================================================================
# Issue #1287: Search Service Protocol
# =============================================================================


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
        show_parsed: bool = True,
        context: "OperationContext | None" = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any: ...

    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: "OperationContext | None" = None,
    ) -> builtins.list[str]: ...

    def glob_batch(
        self,
        patterns: builtins.list[str],
        path: str = "/",
        context: "OperationContext | None" = None,
    ) -> dict[str, builtins.list[str]]: ...

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: "OperationContext | None" = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
    ) -> builtins.list[dict[str, Any]]: ...

    # ── Async operations (semantic search requires I/O) ─────────────────

    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]: ...

    async def semantic_search_index(
        self,
        path: str = "/",
        recursive: bool = True,
    ) -> dict[str, int]: ...

    async def semantic_search_stats(self) -> dict[str, Any]: ...
