"""Secrets audit API models.

Issue #997: Response models for the secrets audit log endpoints.
"""

from __future__ import annotations

from nexus.server.api.v2.models.base import ApiModel


class SecretsAuditEventResponse(ApiModel):
    """Single secrets audit log entry."""

    id: str
    record_hash: str
    created_at: str  # ISO-8601
    event_type: str
    actor_id: str
    provider: str | None = None
    credential_id: str | None = None
    token_family_id: str | None = None
    zone_id: str
    ip_address: str | None = None
    details: str | None = None
    metadata_hash: str | None = None


class SecretsAuditEventListResponse(ApiModel):
    """Paginated list of secrets audit events."""

    events: list[SecretsAuditEventResponse]
    limit: int
    has_more: bool = False
    total: int | None = None
    next_cursor: str | None = None


class SecretsAuditIntegrityResponse(ApiModel):
    """Result of integrity verification for a single record."""

    record_id: str
    is_valid: bool
    record_hash: str
