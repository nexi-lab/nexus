"""Secrets Audit Log REST API — export-only endpoints.

CRUD/query endpoints have been migrated to RPC services.
Only the streaming export endpoint remains (CSV/JSON download).

    GET /api/v2/secrets-audit/events/export  — Streaming CSV/JSON export

Performance: The endpoint uses plain ``def`` (not ``async def``) so
FastAPI auto-dispatches it to a threadpool. This prevents blocking
the asyncio event loop during synchronous SQLAlchemy I/O.
"""

import csv
import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from nexus.contracts.protocols.secrets_audit_log import SecretsAuditLogProtocol

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


def _build_filters(
    zone_id: str,
    *,
    event_type: str | None = None,
    actor_id: str | None = None,
    provider: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    return {
        "zone_id": zone_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "provider": provider,
        "since": since,
        "until": until,
    }


# --------------------------------------------------------------------------
# Dependency — injected by fastapi_server.py
# --------------------------------------------------------------------------


def get_secrets_audit_logger() -> tuple[SecretsAuditLogProtocol, str]:
    """Placeholder dependency — overridden by fastapi_server.py."""
    raise HTTPException(status_code=500, detail="Secrets audit not configured")


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
    logger_and_zone: tuple[SecretsAuditLogProtocol, str] = Depends(get_secrets_audit_logger),
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
        raise HTTPException(status_code=500, detail="Failed to export secrets audit events") from e

    dicts = [_row_to_dict(r) for r in rows]

    if format == "csv":
        return _csv_response(dicts)
    return _json_response(dicts)


def _csv_stream(rows: list[dict[str, Any]]) -> Iterator[str]:
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "id",
        "created_at",
        "event_type",
        "actor_id",
        "provider",
        "credential_id",
        "token_family_id",
        "zone_id",
        "ip_address",
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
