"""Exchange Audit Log REST API — streaming export only.

All query/aggregation/integrity endpoints migrated to AuditRPCService.
This file retains ONLY the streaming CSV/JSON export which requires
HTTP StreamingResponse (not expressible via gRPC unary).

Issue #1360.
"""

import csv
import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from nexus.contracts.protocols.exchange_audit_log import ExchangeAuditLogProtocol
from nexus.server.api.v2.dependencies import get_exchange_audit_logger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/audit", tags=["audit"])


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Eagerly extract all fields from an ORM row into a plain dict."""
    return {
        "id": row.id,
        "record_hash": row.record_hash,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "protocol": row.protocol,
        "buyer_agent_id": row.buyer_agent_id,
        "seller_agent_id": row.seller_agent_id,
        "amount": str(row.amount),
        "currency": row.currency,
        "status": row.status,
        "application": row.application,
        "zone_id": row.zone_id,
        "trace_id": row.trace_id,
        "metadata_hash": getattr(row, "metadata_hash", None),
        "transfer_id": row.transfer_id,
    }


def _build_filters(
    zone_id: str,
    *,
    protocol: str | None = None,
    buyer_agent_id: str | None = None,
    seller_agent_id: str | None = None,
    status: str | None = None,
    application: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Build a filter dict, always including zone_id."""
    return {
        "zone_id": zone_id,
        "protocol": protocol,
        "buyer_agent_id": buyer_agent_id,
        "seller_agent_id": seller_agent_id,
        "status": status,
        "application": application,
        "since": since,
        "until": until,
    }


# --------------------------------------------------------------------------
# Export (CSV / JSON) — MUST_STAY: uses StreamingResponse
# --------------------------------------------------------------------------


@router.get("/transactions/export")
def export_transactions(
    format: str = Query("json", description="Export format: json or csv"),  # noqa: A002
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    protocol: str | None = Query(None),
    buyer_agent_id: str | None = Query(None),
    seller_agent_id: str | None = Query(None),
    status: str | None = Query(None),
    application: str | None = Query(None),
    logger_and_zone: tuple[ExchangeAuditLogProtocol, str] = Depends(get_exchange_audit_logger),
) -> StreamingResponse:
    """Export transactions as CSV or JSON (streaming)."""
    audit_logger, zone_id = logger_and_zone

    filters = _build_filters(
        zone_id,
        protocol=protocol,
        buyer_agent_id=buyer_agent_id,
        seller_agent_id=seller_agent_id,
        status=status,
        application=application,
        since=since,
        until=until,
    )

    try:
        rows = audit_logger.iter_transactions(filters=filters)
    except Exception as e:
        logger.error("Audit export error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to export transactions") from e

    dicts = [_row_to_dict(r) for r in rows]

    if format == "csv":
        return _csv_response(dicts)
    return _json_response(dicts)


def _csv_stream(rows: list[dict[str, Any]]) -> Iterator[str]:
    """Yield CSV data in chunks to enable true streaming."""
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "id",
        "created_at",
        "protocol",
        "buyer_agent_id",
        "seller_agent_id",
        "amount",
        "currency",
        "status",
        "application",
        "zone_id",
        "trace_id",
        "transfer_id",
        "record_hash",
    ]
    writer.writerow(headers)
    yield output.getvalue()
    output.seek(0)
    output.truncate()

    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["created_at"],
                row["protocol"],
                row["buyer_agent_id"],
                row["seller_agent_id"],
                row["amount"],
                row["currency"],
                row["status"],
                row["application"],
                row["zone_id"],
                row.get("trace_id") or "",
                row.get("transfer_id") or "",
                row["record_hash"],
            ]
        )
        yield output.getvalue()
        output.seek(0)
        output.truncate()


def _csv_response(rows: list[dict[str, Any]]) -> StreamingResponse:
    """Build a truly streaming CSV response."""
    return StreamingResponse(
        _csv_stream(rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_transactions.csv"},
    )


def _json_response(rows: list[dict[str, Any]]) -> StreamingResponse:
    """Build a JSON response from pre-extracted dicts."""
    content = json.dumps({"transactions": rows})
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=audit_transactions.json"},
    )
