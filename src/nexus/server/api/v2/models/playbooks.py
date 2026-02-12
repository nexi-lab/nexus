"""Playbook request/response models for API v2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class PlaybookCreateRequest(ApiModel):
    """Request for POST /api/v2/playbooks."""

    name: str = Field(..., description="Playbook name")
    description: str | None = Field(None, description="Playbook description")
    scope: Literal["agent", "user", "zone", "global"] = Field("agent", description="Playbook scope")
    visibility: Literal["private", "shared", "public"] = Field(
        "private", description="Playbook visibility"
    )
    initial_strategies: list[dict[str, Any]] | None = Field(None, description="Initial strategies")


class PlaybookUpdateRequest(ApiModel):
    """Request for PUT /api/v2/playbooks/{id}."""

    strategies: list[dict[str, Any]] | None = Field(None, description="Updated strategies")
    metadata: dict[str, Any] | None = Field(None, description="Updated metadata")
    increment_version: bool = Field(True, description="Increment version number")


class PlaybookUsageRequest(ApiModel):
    """Request for POST /api/v2/playbooks/{id}/usage."""

    success: bool = Field(..., description="Whether the usage was successful")
    improvement_score: float | None = Field(None, ge=0.0, le=1.0, description="Improvement score")


class PlaybookResponse(ApiModel):
    """Response model for playbook objects."""

    playbook_id: str
    name: str
    description: str | None = None
    version: int = 1
    scope: str
    visibility: str
    usage_count: int = 0
    success_rate: float | None = None
    strategies: list[dict[str, Any]] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PlaybookGetResponse(ApiModel):
    """Response for GET /api/v2/playbooks/{id}."""

    playbook: dict[str, Any]


class PlaybookCreateResponse(ApiModel):
    """Response for POST /api/v2/playbooks."""

    playbook_id: str
    status: str = "created"
