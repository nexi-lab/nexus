"""Search brick protocols for dependency inversion (Issue #1520, #2075, #2663).

Defines SearchableProtocol and re-exports SearchBackendProtocol.

FileReaderProtocol has been moved to ``nexus.contracts.protocols.file_reader``
(Issue #2036) for LEGO compliance.  It is re-exported here for backward
compatibility within the brick.
"""

from typing import Any, Protocol, runtime_checkable

from nexus.bricks.search.results import BaseSearchResult
from nexus.contracts.protocols.file_reader import FileReaderProtocol

# Re-export for backward compat within the brick
__all__ = ["FileReaderProtocol", "SearchableProtocol"]


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
