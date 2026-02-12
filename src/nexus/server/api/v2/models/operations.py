"""Operation request/response models for API v2 (Issue #1197, #1198)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class OperationResponse(ApiModel):
    """Single operation entry."""

    id: str
    agent_id: str | None = None
    operation_type: str
    path: str
    new_path: str | None = None
    status: str
    timestamp: str  # ISO-8601
    metadata: dict[str, Any] | None = None


class OperationListResponse(ApiModel):
    """Response for GET /api/v2/operations.

    In offset mode: offset, has_more are always set. total is only
    populated when include_total=true (opt-in to avoid COUNT query).
    In cursor mode: next_cursor is populated (offset/total are None).
    """

    operations: list[OperationResponse]
    limit: int
    has_more: bool = False
    offset: int | None = None
    total: int | None = None
    next_cursor: str | None = None


class AgentActivityResponse(ApiModel):
    """Response for GET /api/v2/operations/agents/{agent_id}/activity.

    Aggregated activity summary for a specific agent within a time window.
    All fields are scoped to the since filter (default: last 24h).

    Issue #1198: Add Agent Activity Summary endpoint.
    """

    agent_id: str
    total_operations: int = 0
    operations_by_type: dict[str, int] = Field(default_factory=dict)
    recent_paths: list[str] = Field(default_factory=list)
    last_active: str | None = None  # ISO-8601
    first_seen: str | None = None  # ISO-8601
