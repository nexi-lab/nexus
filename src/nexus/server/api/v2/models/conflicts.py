"""Conflict request/response models for API v2 (Issue #1130)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class ConflictDetailResponse(ApiModel):
    """Full conflict record representation."""

    conflict_id: str
    path: str
    backend_name: str
    zone_id: str
    strategy: str
    outcome: str
    nexus_content_hash: str | None = None
    nexus_mtime: str | None = None
    nexus_size: int | None = None
    backend_content_hash: str | None = None
    backend_mtime: str | None = None
    backend_size: int | None = None
    conflict_copy_path: str | None = None
    status: str
    resolved_at: str | None = None


class ConflictListResponse(ApiModel):
    """Response for GET /api/v2/sync/conflicts."""

    conflicts: list[ConflictDetailResponse]
    total: int


class ConflictResolveRequest(ApiModel):
    """Request for POST /api/v2/sync/conflicts/{id}/resolve."""

    outcome: Literal["nexus_wins", "backend_wins"] = Field(
        ..., description="Chosen resolution outcome"
    )


class ConflictResolveResponse(ApiModel):
    """Response for POST /api/v2/sync/conflicts/{id}/resolve."""

    conflict_id: str
    status: str
