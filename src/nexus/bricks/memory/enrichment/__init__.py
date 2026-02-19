"""Memory enrichment pipeline for Memory brick.

Extracted from services/memory/enrichment.py as part of Memory brick (Issue #2128).

The enrichment pipeline provides:
- Embedding generation
- Entity extraction
- Temporal metadata extraction
- Relationship extraction
- Temporal stability classification
- Content resolution (coreferences, temporal expressions)

All enrichment steps are failure-tolerant and independently testable.
"""

from __future__ import annotations

from nexus.bricks.memory.enrichment.pipeline import (
    EnrichmentFlags,
    EnrichmentPipeline,
    EnrichmentResult,
)

__all__ = [
    "EnrichmentFlags",
    "EnrichmentPipeline",
    "EnrichmentResult",
]
