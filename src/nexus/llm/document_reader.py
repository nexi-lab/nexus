"""Backward compatibility — moved to nexus.services.llm_document_reader (Issue #1521).

This module re-exports from the new location for import compatibility.
Consumers should update imports to nexus.services.llm_document_reader.
"""

from nexus.services.llm_document_reader import (
    LLMDocumentReader,
    ReadChunk,
)

__all__ = ["LLMDocumentReader", "ReadChunk"]
