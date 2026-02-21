"""Artifact indexing configuration (Issue #1861).

Frozen dataclass controlling which indexing targets are enabled
and the maximum content size before truncation.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArtifactIndexConfig:
    """Configuration for artifact auto-indexing.

    Attributes:
        memory_enabled: Index artifact text into semantic memory.
        tool_enabled: Index tool schemas into ToolIndex for discovery.
        graph_enabled: Index entities into the knowledge graph.
        max_content_bytes: Truncate extracted content beyond this limit.
    """

    memory_enabled: bool = True
    tool_enabled: bool = True
    graph_enabled: bool = True
    max_content_bytes: int = 100_000
