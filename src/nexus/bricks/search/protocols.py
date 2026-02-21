"""Search brick protocols for dependency inversion (Issue #1520, #2075).

Defines SearchableProtocol — the minimal interface that
GraphEnhancedRetriever needs, replacing the concrete SemanticSearch dependency.

FileReaderProtocol has been moved to ``nexus.services.protocols.file_reader``
(Issue #2036) for LEGO compliance.  It is re-exported here for backward
compatibility within the brick.

This enables:
- Zero kernel imports in the search brick
- Easy testing with mock file readers
- Pluggable backends (local, GCS, S3, etc.)
"""

from typing import Any, Protocol, runtime_checkable

from nexus.bricks.search.results import BaseSearchResult
from nexus.services.protocols.file_reader import FileReaderProtocol

# Re-export for backward compat within the brick
__all__ = ["FileReaderProtocol", "SearchableProtocol"]


@runtime_checkable
class SearchableProtocol(Protocol):
    """Minimal search interface used by GraphEnhancedRetriever.

    Replaces the concrete SemanticSearch dependency with a duck-typed contract.
    Both QueryService and DaemonSemanticSearchWrapper satisfy this protocol.
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
