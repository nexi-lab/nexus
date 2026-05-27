"""Aspect, catalog, and replay request/response models (Issue #2930)."""

from typing import Any

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class AspectResponse(ApiModel):
    """Single aspect entry."""

    entity_urn: str
    aspect_name: str
    version: int
    payload: dict[str, Any]
    created_by: str = "system"
    created_at: str | None = None


class AspectListResponse(ApiModel):
    """Response for GET /api/v2/aspects/{urn}."""

    entity_urn: str
    aspects: list[str]


class AspectHistoryResponse(ApiModel):
    """Version history for a single aspect."""

    entity_urn: str
    aspect_name: str
    versions: list[AspectResponse]


class PutAspectRequest(ApiModel):
    """Request body for PUT /api/v2/aspects/{urn}/{name}."""

    payload: dict[str, Any]
    created_by: str = "system"


class CatalogSchemaResponse(ApiModel):
    """Response for GET /api/v2/catalog/schema/{path}."""

    entity_urn: str
    path: str
    schema_: dict[str, Any] | None = Field(None, alias="schema")

    model_config = {"extra": "ignore", "populate_by_name": True}


class ColumnSearchResult(ApiModel):
    """Single column search match."""

    entity_urn: str
    column_name: str
    column_type: str
    path: str | None = None
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    model_config = {"extra": "ignore", "populate_by_name": True}


class ColumnSearchResponse(ApiModel):
    """Response for GET /api/v2/catalog/search."""

    results: list[ColumnSearchResult]
    total: int
    capped: bool = False


class ReplayResponse(ApiModel):
    """Response for GET /api/v2/ops/replay."""

    records: list[dict[str, Any]]
    next_cursor: int | None = None
    has_more: bool = False


class ReindexRequest(ApiModel):
    """Request body for POST /api/v2/admin/reindex."""

    target: str = "all"
    from_sequence: int | None = None
    batch_size: int = 500
    zone_id: str | None = None
    dry_run: bool = False


class ReindexResponse(ApiModel):
    """Response for POST /api/v2/admin/reindex."""

    target: str
    total: int
    processed: int = 0
    errors: int = 0
    last_sequence: int = 0
    dry_run: bool = False
    # Issue #4241: surface that the search-daemon refresh was *enqueued*
    # for these paths. Round-1 review (codex finding MEDIUM): the consumer
    # loop drains asynchronously, so a non-zero count means "queued", not
    # "completed". Operators who need a hard barrier should poll
    # /api/v2/search/stats for ``last_index_refresh`` advancing past
    # ``search_refresh_enqueued_at``, or sleep until BM25 returns the
    # expected hits before declaring success.
    search_paths_enqueued: int = 0
    search_refresh_enqueued_at: float | None = None
    # Round-4 review (codex MEDIUM): explicit signal when one or more
    # enqueue calls failed (backend down, queue full, etc.). Without
    # this, a partial failure returned processed=N, errors=0, and only
    # a lower enqueued count — operators could miss that part of the
    # replay never reached the search index. ``failed_paths`` is
    # capped at 25 entries to bound response size.
    search_enqueue_errors: int = 0
    search_enqueue_failed_paths: list[str] = []
