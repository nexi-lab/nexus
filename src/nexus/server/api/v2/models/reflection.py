"""Reflection & Curation request/response models for API v2."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class ReflectRequest(ApiModel):
    """Request for POST /api/v2/reflect."""

    trajectory_id: str = Field(..., description="Trajectory to reflect on")
    context: str | None = Field(None, description="Additional context")
    reflection_prompt: str | None = Field(None, description="Custom reflection prompt")


class CurateRequest(ApiModel):
    """Request for POST /api/v2/curate."""

    playbook_id: str = Field(..., description="Target playbook")
    reflection_memory_ids: list[str] = Field(..., description="Reflection memories to curate")
    merge_threshold: float = Field(0.7, ge=0.0, le=1.0, description="Strategy merge threshold")


class CurateBulkRequest(ApiModel):
    """Request for POST /api/v2/curate/bulk."""

    playbook_id: str = Field(..., description="Target playbook")
    trajectory_ids: list[str] = Field(..., description="Trajectories to curate from")


class ReflectionResponse(ApiModel):
    """Response model for reflection results."""

    memory_id: str
    trajectory_id: str
    helpful_strategies: list[dict[str, Any]]
    harmful_patterns: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    confidence: float


class CurationResponse(ApiModel):
    """Response for curation operations."""

    playbook_id: str
    strategies_added: int
    strategies_merged: int
    strategies_total: int
