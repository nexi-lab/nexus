"""Lineage API request/response models (Issue #3417)."""

from typing import Any

from nexus.server.api.v2.models.base import ApiModel


class UpstreamEntry(ApiModel):
    """A single upstream dependency in lineage."""

    path: str
    version: int = 0
    content_id: str = ""
    access_type: str = "content"


class LineageResponse(ApiModel):
    """Response for GET /api/v2/lineage/{urn}."""

    entity_urn: str
    upstream: list[dict[str, Any]] = []
    agent_id: str = ""
    agent_generation: int | None = None
    operation: str = ""
    duration_ms: int | None = None
    truncated: bool = False


class DownstreamEntry(ApiModel):
    """A single downstream dependent in impact analysis."""

    downstream_urn: str
    downstream_path: str | None = None
    upstream_version: int = 0
    upstream_etag: str = ""
    access_type: str = "content"
    agent_id: str = ""
    created_at: str | None = None


class DownstreamResponse(ApiModel):
    """Response for GET /api/v2/lineage/downstream."""

    upstream_path: str
    downstream: list[DownstreamEntry] = []
    total: int = 0


class StaleEntry(ApiModel):
    """A single stale downstream in staleness detection."""

    downstream_urn: str
    downstream_path: str | None = None
    recorded_version: int = 0
    recorded_etag: str = ""
    current_version: int = 0
    current_etag: str = ""
    agent_id: str = ""


class StaleResponse(ApiModel):
    """Response for GET /api/v2/lineage/stale."""

    upstream_path: str
    current_version: int = 0
    current_etag: str = ""
    stale: list[StaleEntry] = []
    total: int = 0


class PutLineageRequest(ApiModel):
    """Request body for PUT /api/v2/lineage/{urn} (explicit declaration)."""

    upstream: list[UpstreamEntry]
    agent_id: str
    agent_generation: int | None = None


class ScopeRequest(ApiModel):
    """Request body for POST /api/v2/lineage/scope/begin and end."""

    agent_id: str
    agent_generation: int | None = None
    scope_id: str


class ScopeResponse(ApiModel):
    """Response for scope operations."""

    agent_id: str
    scope_id: str
    active_scope: str
    reads_count: int = 0
