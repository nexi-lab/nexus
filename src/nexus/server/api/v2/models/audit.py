"""Audit-related API models.

Issue #1360: Exchange audit log query & export API.
"""

from __future__ import annotations

from typing import Any

from nexus.server.api.v2.models.base import ApiModel


class AuditTransactionResponse(ApiModel):
    """Single exchange audit log entry."""

    id: str
    record_hash: str
    created_at: str  # ISO-8601
    protocol: str
    buyer_agent_id: str
    seller_agent_id: str
    amount: str  # Decimal as string for precision
    currency: str
    status: str
    application: str
    zone_id: str
    trace_id: str | None = None
    metadata_hash: str | None = None
    transfer_id: str | None = None


class AuditTransactionListResponse(ApiModel):
    """Paginated list of audit transactions."""

    transactions: list[AuditTransactionResponse]
    limit: int
    has_more: bool = False
    total: int | None = None
    next_cursor: str | None = None


class AuditAggregationResponse(ApiModel):
    """Aggregation results for audit transactions."""

    total_volume: str  # Decimal as string
    tx_count: int
    top_buyers: list[dict[str, Any]]
    top_sellers: list[dict[str, Any]]


class AuditIntegrityResponse(ApiModel):
    """Result of integrity verification for a single record."""

    record_id: str
    is_valid: bool
    record_hash: str
