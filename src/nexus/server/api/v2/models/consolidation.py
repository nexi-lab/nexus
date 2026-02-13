"""Consolidation request/response models for API v2."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class ConsolidateRequest(ApiModel):
    """Request for POST /api/v2/consolidate."""

    memory_ids: list[str] | None = Field(None, description="Specific memories to consolidate")
    beta: float = Field(0.7, ge=0.0, le=1.0, description="Semantic weight (SimpleMem)")
    lambda_decay: float = Field(0.1, ge=0.0, description="Temporal decay rate")
    affinity_threshold: float = Field(0.85, ge=0.0, le=1.0, description="Clustering threshold")
    importance_max: float = Field(0.5, ge=0.0, le=1.0, description="Max importance for candidates")
    memory_type: str | None = Field(None, description="Filter by memory type")
    namespace: str | None = Field(None, description="Filter by namespace")
    limit: int = Field(
        100,
        ge=1,
        le=200,
        description=("Max memories to process. Consolidation is O(n^2) on this value."),
    )


class HierarchyBuildRequest(ApiModel):
    """Request for POST /api/v2/consolidate/hierarchy."""

    memory_ids: list[str] | None = Field(None, description="Specific memories")
    max_levels: int = Field(3, ge=1, le=10, description="Maximum hierarchy levels")
    cluster_threshold: float = Field(0.6, ge=0.0, le=1.0, description="Clustering threshold")
    beta: float = Field(0.7, ge=0.0, le=1.0, description="Semantic weight")
    lambda_decay: float = Field(0.1, ge=0.0, description="Temporal decay rate")
    time_unit_hours: float = Field(24.0, description="Time unit for decay calculation")


class DecayRequest(ApiModel):
    """Request for POST /api/v2/consolidate/decay."""

    decay_factor: float = Field(0.95, ge=0.0, le=1.0, description="Decay factor per period")
    min_importance: float = Field(0.1, ge=0.0, le=1.0, description="Minimum importance floor")
    batch_size: int = Field(1000, ge=1, le=10000, description="Batch size for processing")


class ConsolidationResponse(ApiModel):
    """Response for consolidation operations."""

    clusters_formed: int
    total_consolidated: int
    archived_count: int = 0
    results: list[dict[str, Any]]


class HierarchyResponse(ApiModel):
    """Response for hierarchy operations."""

    total_memories: int
    total_abstracts_created: int
    max_level_reached: int
    levels: dict[str, Any]
    statistics: dict[str, Any] | None = None


class DecayResponse(ApiModel):
    """Response for decay operations."""

    success: bool
    updated: int
    skipped: int
    processed: int
    error: str | None = None
