"""Artifact indexer protocol and content dataclass (Issue #1861).

Defines the contract for artifact indexing adapters and the immutable
content container passed between extractors and indexers.

Storage Affinity: N/A — protocol-only, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """Immutable container for extracted artifact content.

    Passed from extractors to indexer adapters.  Follows the same
    ``frozen=True, slots=True`` pattern as ``HookContext``.

    Attributes:
        text: Extracted text content (may be truncated).
        metadata: Merged artifact + part metadata.
        artifact_id: Unique artifact identifier.
        task_id: Owning task identifier.
        zone_id: Zone/organization scope.
    """

    text: str
    metadata: dict[str, Any]
    artifact_id: str
    task_id: str
    zone_id: str


@runtime_checkable
class ArtifactIndexerProtocol(Protocol):
    """Contract for artifact indexing adapters.

    Each adapter (memory, tool, graph) implements this protocol.
    The ``index`` method receives extracted content and writes to
    the downstream system.  Errors are handled internally
    (log-and-suppress); callers should not expect exceptions.
    """

    async def index(self, content: ArtifactContent) -> None: ...
