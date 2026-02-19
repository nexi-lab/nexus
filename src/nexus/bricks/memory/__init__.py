"""Nexus Memory Brick - AI Agent Memory Management.

This module provides memory storage, querying, and lifecycle management for
AI agents using the brick architecture pattern.

Architecture:
    MemoryBrick implements MemoryProtocol with zero cross-brick imports.
    All dependencies injected via constructor (DI pattern).

    RecordStore handles: memory metadata, versioning, state transitions
    ObjectStore handles: content blobs (CAS), embeddings
    GraphStore handles: entity relationships (optional)

Key Features:
    - Store and retrieve agent memories with semantic search
    - Version history and rollback capabilities
    - State lifecycle (active, inactive, invalid)
    - Permission enforcement via ReBAC
    - Temporal queries (as_of, during, before/after)
    - Importance decay over time
    - Enrichment pipeline (entities, relationships, stability classification)

Example:
    >>> from nexus.bricks.memory import MemoryBrick
    >>> memory = MemoryBrick(record_store=..., permission_enforcer=...)
    >>> memory_id = memory.store(content="Important fact", scope="user")
    >>> result = memory.get(memory_id)

Related: Issue #2128 (Memory brick extraction), NEXUS-LEGO-ARCHITECTURE.md
"""

from nexus.bricks.memory.response_models import (
    BatchOperationResult,
    MemoryDetailResponse,
    MemoryListResponse,
    MemoryQueryResponse,
    MemoryRetrieveResponse,
    MemorySearchResponse,
)
from nexus.bricks.memory.service import MemoryBrick, RetentionPolicy
from nexus.services.protocols.memory import MemoryProtocol

__all__ = [
    # Main brick class
    "MemoryBrick",
    # Configuration
    "RetentionPolicy",
    # Protocol (re-export for convenience)
    "MemoryProtocol",
    # Response models
    "MemoryDetailResponse",
    "MemoryListResponse",
    "MemoryQueryResponse",
    "MemoryRetrieveResponse",
    "MemorySearchResponse",
    "BatchOperationResult",
]
