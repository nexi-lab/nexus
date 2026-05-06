"""Search brick protocols for dependency inversion (Issue #1520, #2075, #2663).

Defines SearchableProtocol (daemon-facing facade) and SearchBackend
(backend-facing primitive). Re-exports FileReaderProtocol for backward compat
within the brick.
"""

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from nexus.bricks.search.results import BaseSearchResult
from nexus.contracts.protocols.file_reader import FileReaderProtocol

# Re-export for backward compat within the brick
__all__ = ["FileReaderProtocol", "SearchableProtocol", "SearchBackend"]


@runtime_checkable
class SearchableProtocol(Protocol):
    """Minimal search interface used by graph search and callers.

    Both SearchDaemon and DaemonSemanticSearchWrapper satisfy this protocol.
    """

    embedding_provider: Any

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        search_mode: str = "semantic",
        alpha: float = 0.5,
        **kwargs: Any,
    ) -> list[BaseSearchResult]: ...


@runtime_checkable
class SearchBackend(Protocol):
    """Unified backend contract for keyword + semantic search.

    Hybrid fusion is the daemon's responsibility (see fusion.rrf_fusion);
    backends only expose the two single-mode primitives.
    """

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int: ...

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int: ...

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int: ...

    async def keyword_search(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]: ...

    async def semantic_search(
        self,
        query_vector: Sequence[float],
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]: ...

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...
