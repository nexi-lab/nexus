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
    # #4241 / #4736: for ``target`` in {all, search} the route drives the
    # search plugin SYNCHRONOUSLY — deletes evict, updates are read and
    # indexed in bounded IndexDocuments batches — so these are completion
    # counts, not queue depths (the pre-P12 ``search_paths_enqueued`` /
    # ``search_refresh_enqueued_at`` / ``search_enqueue_*`` fields described
    # a consumer queue that no longer exists).  Every replayed path lands in
    # exactly one of indexed / deleted / skipped / failed; the path lists
    # are capped at 25 entries to bound the response body.
    search_paths_indexed: int = 0
    search_paths_deleted: int = 0
    search_paths_skipped: int = 0
    # Histogram of skip kinds: empty | non_text | oversize.
    search_skip_reasons: dict[str, int] = {}
    search_skipped_paths: list[str] = []
    search_index_errors: int = 0
    search_index_failed_paths: list[str] = []
    # First failure text — a down plugin shows up here, not just as a count.
    search_index_error: str | None = None
    # Highest plugin ``index_seq`` this pass produced; ``/search/stats``
    # ``last_index_seq >= search_index_seq`` ⇒ everything indexed is served.
    search_index_seq: int | None = None
    # Epoch seconds when the last plugin call RETURNED (a completion time,
    # unlike the old enqueue timestamp).
    search_indexed_at: float | None = None
