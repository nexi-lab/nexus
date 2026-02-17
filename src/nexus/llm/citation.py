"""Backward compatibility — moved to nexus.services.llm_citation (Issue #1521).

This module re-exports from the new location for import compatibility.
Consumers should update imports to nexus.services.llm_citation.
"""

from nexus.services.llm_citation import (
    Citation,
    CitationExtractor,
    DocumentReadResult,
)

__all__ = ["Citation", "CitationExtractor", "DocumentReadResult"]
