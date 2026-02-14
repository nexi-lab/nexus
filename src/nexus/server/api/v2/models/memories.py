"""Memory request/response models for API v2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class MemoryStoreRequest(ApiModel):
    """Request model for POST /api/v2/memories."""

    content: str | dict[str, Any] = Field(..., description="Memory content (text or JSON)")
    scope: Literal["agent", "user", "zone", "global", "session"] = Field(
        "user", description="Memory scope"
    )
    memory_type: str | None = Field(
        None,
        description="Memory type (fact, preference, experience, strategy, etc.)",
    )
    importance: float | None = Field(None, ge=0.0, le=1.0, description="Importance score")
    namespace: str | None = Field(None, description="Hierarchical namespace")
    path_key: str | None = Field(None, description="Unique key within namespace for upsert")
    state: Literal["active", "inactive"] = Field("active", description="Memory state")
    extract_entities: bool = Field(True, description="Extract named entities")
    extract_temporal: bool = Field(True, description="Extract temporal references")
    extract_relationships: bool = Field(False, description="Extract relationships")
    store_to_graph: bool = Field(False, description="Store entities to knowledge graph")
    valid_at: str | None = Field(None, description="When fact became valid (ISO-8601)")
    classify_stability: bool = Field(True, description="Auto-classify temporal stability")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")


class MemoryUpdateRequest(ApiModel):
    """Request model for PUT /api/v2/memories/{id}."""

    content: str | dict[str, Any] | None = Field(None, description="Updated content")
    importance: float | None = Field(None, ge=0.0, le=1.0, description="Updated importance")
    state: Literal["active", "inactive"] | None = Field(None, description="Updated state")
    namespace: str | None = Field(None, description="Updated namespace")
    metadata: dict[str, Any] | None = Field(None, description="Updated metadata")


class MemorySearchRequest(ApiModel):
    """Request model for POST /api/v2/memories/search."""

    query: str = Field(..., description="Search query")
    scope: str | None = Field(None, description="Filter by scope")
    memory_type: str | None = Field(None, description="Filter by memory type")
    limit: int = Field(10, ge=1, le=100, description="Maximum results")
    search_mode: Literal["semantic", "keyword", "hybrid"] = Field(
        "hybrid", description="Search mode"
    )
    after: str | None = Field(None, description="Filter by created after (ISO-8601)")
    before: str | None = Field(None, description="Filter by created before (ISO-8601)")
    during: str | None = Field(None, description="Filter by time period (e.g., 'last week')")
    entity_type: str | None = Field(None, description="Filter by entity type")
    person: str | None = Field(None, description="Filter by person name")
    temporal_stability: str | None = Field(
        None, description="Filter by temporal stability (static, semi_dynamic, dynamic)"
    )


class MemoryQueryRequest(ApiModel):
    """Request model for POST /api/v2/memories/query (#1185 point-in-time queries)."""

    scope: str | None = Field(None, description="Filter by scope")
    memory_type: str | None = Field(None, description="Filter by memory type")
    namespace: str | None = Field(None, description="Filter by exact namespace")
    namespace_prefix: str | None = Field(None, description="Filter by namespace prefix")
    state: str | None = Field("active", description="Filter by state (active, inactive, all)")
    limit: int | None = Field(None, ge=1, le=1000, description="Maximum results")
    offset: int = Field(0, ge=0, description="Number of results to skip (for pagination)")
    after: str | None = Field(None, description="Filter by created after (ISO-8601)")
    before: str | None = Field(None, description="Filter by created before (ISO-8601)")
    during: str | None = Field(None, description="Filter by time period")
    entity_type: str | None = Field(None, description="Filter by entity type")
    person: str | None = Field(None, description="Filter by person name")
    event_after: str | None = Field(None, description="Filter by event date >= (ISO-8601)")
    event_before: str | None = Field(None, description="Filter by event date <= (ISO-8601)")
    include_invalid: bool = Field(False, description="Include invalidated memories")
    as_of_event: str | None = Field(
        None, description="What was TRUE at time X? (ISO-8601, filters by valid_at/invalid_at)"
    )
    as_of_system: str | None = Field(
        None, description="What did SYSTEM KNOW at time X? (ISO-8601, filters by created_at)"
    )
    include_superseded: bool = Field(False, description="Include superseded (old version) memories")
    temporal_stability: str | None = Field(
        None, description="Filter by temporal stability (static, semi_dynamic, dynamic)"
    )


class MemoryBatchStoreRequest(ApiModel):
    """Request model for POST /api/v2/memories/batch."""

    memories: list[MemoryStoreRequest] = Field(..., description="List of memories to store")


class MemoryResponse(ApiModel):
    """Response model for memory objects."""

    memory_id: str
    content: str | dict[str, Any]
    content_hash: str | None = None
    scope: str
    memory_type: str | None = None
    importance: float | None = None
    importance_effective: float | None = None
    state: str
    namespace: str | None = None
    path_key: str | None = None
    access_count: int = 0
    entities: list[dict[str, Any]] | None = None
    temporal_refs: list[dict[str, Any]] | None = None
    temporal_stability: str | None = None
    stability_confidence: float | None = None
    estimated_ttl_days: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemoryGetResponse(ApiModel):
    """Response for GET /api/v2/memories/{id}."""

    memory: dict[str, Any]
    versions: list[dict[str, Any]] | None = None


class MemoryStoreResponse(ApiModel):
    """Response for POST /api/v2/memories."""

    memory_id: str
    status: str = "created"


class MemoryBatchStoreResponse(ApiModel):
    """Response for POST /api/v2/memories/batch."""

    stored: int
    failed: int
    memory_ids: list[str]
    errors: list[dict[str, Any]] | None = None


class MemoryVersionHistoryResponse(ApiModel):
    """Response for GET /api/v2/memories/{id}/history."""

    memory_id: str
    current_version: int
    versions: list[dict[str, Any]]
