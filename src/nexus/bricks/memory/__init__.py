"""Memory brick — AI agent memory management (Issue #2177).

Public API for the Memory brick, providing CRUD operations, state lifecycle,
versioning, enrichment pipeline, and semantic search for agent memories.

Extracted from services/memory/ to follow the LEGO brick architecture with
protocol boundaries and dependency injection.
"""

from nexus.bricks.memory.enrichment import EnrichmentFlags, EnrichmentPipeline, EnrichmentResult
from nexus.bricks.memory.response_models import (
    BatchOperationResult,
    MemoryDetailResponse,
    MemoryListResponse,
    MemoryQueryResponse,
    MemoryRetrieveResponse,
    MemorySearchResponse,
)
from nexus.bricks.memory.router import MemoryViewRouter
from nexus.bricks.memory.service import Memory, get_effective_importance
from nexus.bricks.memory.state import MemoryStateManager
from nexus.bricks.memory.versioning import MemoryVersioning

__all__ = [
    # Core API
    "Memory",
    "MemoryViewRouter",
    "MemoryStateManager",
    "MemoryVersioning",
    # Enrichment
    "EnrichmentFlags",
    "EnrichmentPipeline",
    "EnrichmentResult",
    # Response models
    "BatchOperationResult",
    "MemoryDetailResponse",
    "MemoryListResponse",
    "MemoryQueryResponse",
    "MemoryRetrieveResponse",
    "MemorySearchResponse",
    # Utilities
    "get_effective_importance",
]
