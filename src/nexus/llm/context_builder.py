"""Backward compatibility — moved to nexus.services.llm_context_builder (Issue #1521).

This module re-exports from the new location for import compatibility.
Consumers should update imports to nexus.services.llm_context_builder.
"""

from nexus.services.llm_context_builder import (
    AdaptiveRetrievalConfig,
    ChunkLike,
    ContextBuilder,
)

__all__ = ["AdaptiveRetrievalConfig", "ChunkLike", "ContextBuilder"]
