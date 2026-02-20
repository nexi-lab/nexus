"""Artifact auto-indexing brick (Issue #1861).

Indexes A2A artifact content into three downstream systems:
- Semantic memory (Memory.store)
- Tool discovery (ToolIndex.add_tool)
- Knowledge graph (GraphStore.add_entity)

Public API:
    ArtifactContent — Immutable extracted content container.
    ArtifactIndexerProtocol — Contract for indexing adapters.
    ArtifactIndexConfig — Per-target enable/disable + max content bytes.
"""

from nexus.bricks.artifact_index.config import ArtifactIndexConfig
from nexus.bricks.artifact_index.protocol import ArtifactContent, ArtifactIndexerProtocol

__all__ = [
    "ArtifactContent",
    "ArtifactIndexConfig",
    "ArtifactIndexerProtocol",
]
