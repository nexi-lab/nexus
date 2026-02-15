"""Secrets Audit Log REST API endpoints.

Issue #997: Query & export API for the immutable secrets/credential
audit trail. Provides filtered listing, CSV/JSON export, single-record
lookup, and integrity verification.

All endpoints are scoped to the authenticated user's zone_id.

Performance: All endpoints use plain ``def`` (not ``async def``) so
FastAPI auto-dispatches them to a threadpool. This prevents blocking
the asyncio event loop during synchronous SQLAlchemy I/O.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from nexus.server.api.v2.models.secrets_audit import (
    SecretsAuditEventListResponse,
    SecretsAuditEventResponse,
    SecretsAuditIntegrityResponse,
)
from nexus.storage.secrets_audit_logger import SecretsAuditLogger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/secrets-audit", tags=["secrets-audit"])


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert ORM row to plain dict (avoid DetachedInstanceError)."""
    return {
        "id": row.id,
        "record_hash": row.record_hash,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "event_type": row.event_type,
        "actor_id": row.actor_id,
        "provider": row.provider,
        "credential_id": row.credential_id,
        "token_family_id": row.token_family_id,
        "zone_id": row.zone_id,
        "ip_address": row.ip_address,
        "details": row.details,
        "metadata_hash": row.metadata_hash,
    }


def _dict_to_response(d: dict[str, Any]) -> SecretsAuditEventResponse:
    return SecretsAuditEventResponse(**d)


def _build_filters(
    zone_id: str,
    *,
    event_type: str | None = None,
    actor_id: str | None = None,
    provider: str | None = None,
    credential_id: str | None = None,
    token_family_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    return {
        "zone_id": zone_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "provider": provider,
        "credential_id": credential_id,
        "token_family_id": token_family_id,
        "since": since,
        "until": until,
    }


# --------------------------------------------------------------------------
# Dependency — injected by fastapi_server.py
# --------------------------------------------------------------------------


def get_secrets_audit_logger() -> tuple[SecretsAuditLogger, str]:
    """Placeholder dependency — overridden by fastapi_server.py."""
    raise HTTPException(status_code=500, detail="Secrets audit not configured")


# --------------------------------------------------------------------------
# List events
# --------------------------------------------------------------------------


@router.get("/events")
def list_events(
    since: datetime | None = Query(None, description="Events after this time (ISO-8601)"),
    until: datetime | None = Query(None, description="Events before this time (ISO-8601)"),
    event_type: str | None = Query(None, description="Filter by event type"),
    actor_id: str | None = Query(None, description="Filter by actor ID"),
    provider: str | None = Query(None, description="Filter by OAuth provider"),
    credential_id: str | None = Query(None, description="Filter by credential ID"),
    token_family_id: str | None = Query(None, description="Filter by token family ID"),
    limit: int = Query(100, ge=1, le=1000, description="Page size"),
    cursor: str | None = Query(None, description="Cursor from previous response"),
    include_total: bool = Query(False, description="Include total count"),
    logger_and_zone: tuple[SecretsAuditLogger, str] = Depends(get_secrets_audit_logger),
) -> SecretsAuditEventListResponse:
    """List secrets audit events with cursor-based pagination."""
    audit_logger, zone_id = logger_and_zone

    filters = _build_filters(
        zone_id,
        event_type=event_type,
        actor_id=actor_id,
        provider=provider,
        credential_id=credential_id,
        token_family_id=token_family_id,
        since=since,
        until=until,
    )

    try:
        rows, next_cursor = audit_logger.list_events_cursor(
            filters=filters, limit=limit, cursor=cursor
        )
        total = None
        if include_total:
            total = audit_logger.count_events(**filters)

        return SecretsAuditEventListResponse(
            events=[_dict_to_response(_row_to_dict(r)) for r in rows],
            limit=limit,
            has_more=next_cursor is not None,
            total=total,
            next_cursor=next_cursor,
        )
    except Exception as e:
        logger.error("Secrets audit query error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to query secrets audit events"
        ) from e


# --------------------------------------------------------------------------
# Export (CSV / JSON)
# --------------------------------------------------------------------------


@router.get("/events/export")
def export_events(
    format: Literal["json", "csv"] = Query("json", description="Export format: json or csv"),  # noqa: A002
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    event_type: str | None = Query(None),
    actor_id: str | None = Query(None),
    provider: str | None = Query(None),
    limit: int = Query(10_000, ge=1, le=100_000, description="Max rows to export"),
    logger_and_zone: tuple[SecretsAuditLogger, str] = Depends(get_secrets_audit_logger),
) -> StreamingResponse:
    """Export secrets audit events as CSV or JSON (streaming)."""
    audit_logger, zone_id = logger_and_zone

    filters = _build_filters(
        zone_id,
        event_type=event_type,
        actor_id=actor_id,
        provider=provider,
        since=since,
        until=until,
    )

    try:
        rows = audit_logger.iter_events(filters=filters, limit=limit)
    except Exception as e:
        logger.error("Secrets audit export error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to export secrets audit events"
        ) from e

    dicts = [_row_to_dict(r) for r in rows]

    if format == "csv":
        return _csv_response(dicts)
    return _json_response(dicts)


def _csv_stream(rows: list[dict[str, Any]]) -> Iterator[str]:
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "id", "created_at", "event_type", "actor_id", "provider",
        "credential_id", "token_family_id", "zone_id", "ip_address",
        "record_hash",
    ]
    writer.writerow(headers)
    yield output.getvalue()
    output.seek(0)
    output.truncate()

    for row in rows:
        writer.writerow([row.get(h, "") or "" for h in headers])
        yield output.getvalue()
        output.seek(0)
        output.truncate()


def _csv_response(rows: list[dict[str, Any]]) -> StreamingResponse:
    return StreamingResponse(
        _csv_stream(rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=secrets_audit_events.csv"},
    )


def _json_response(rows: list[dict[str, Any]]) -> StreamingResponse:
    content = json.dumps({"events": rows})
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=secrets_audit_events.json"},
    )


# --------------------------------------------------------------------------
# Single event
# --------------------------------------------------------------------------


@router.get("/events/{record_id}")
def get_event(
    record_id: str,
    logger_and_zone: tuple[SecretsAuditLogger, str] = Depends(get_secrets_audit_logger),
) -> SecretsAuditEventResponse:
    """Get a single secrets audit event by ID."""
    audit_logger, zone_id = logger_and_zone

    row = audit_logger.get_event(record_id)
    if row is None or row.zone_id != zone_id:
        raise HTTPException(status_code=404, detail="Audit event not found")
    return _dict_to_response(_row_to_dict(row))


# --------------------------------------------------------------------------
# Integrity verification
# --------------------------------------------------------------------------


@router.get("/integrity/{record_id}")
def verify_integrity(
    record_id: str,
    logger_and_zone: tuple[SecretsAuditLogger, str] = Depends(get_secrets_audit_logger),
) -> SecretsAuditIntegrityResponse:
    """Verify a record's hash matches its data (tamper detection)."""
    audit_logger, zone_id = logger_and_zone

    row = audit_logger.get_event(record_id)
    if row is None or row.zone_id != zone_id:
        raise HTTPException(status_code=404, detail="Audit event not found")

    record_hash = row.record_hash
    is_valid = audit_logger.verify_integrity_from_row(row)
    return SecretsAuditIntegrityResponse(
        record_id=record_id,
        is_valid=is_valid,
        record_hash=record_hash,
    )
