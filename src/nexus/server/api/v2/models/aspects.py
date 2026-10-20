"""Aspect and catalog request/response models."""

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
