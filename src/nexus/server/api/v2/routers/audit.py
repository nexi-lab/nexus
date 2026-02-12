"""Exchange Audit Log REST API endpoints.

Issue #1360 Phase 2: Query & Export API for the immutable transaction
audit trail. Provides filtered listing, aggregations, CSV/JSON export,
single-record lookup, and integrity verification.

All endpoints are scoped to the authenticated user's zone_id.

Performance: All endpoints use plain ``def`` (not ``async def``) so
FastAPI auto-dispatches them to a threadpool.  This prevents blocking
the asyncio event loop during synchronous SQLAlchemy I/O.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from nexus.server.api.v2.dependencies import get_exchange_audit_logger
from nexus.server.api.v2.models import (
    AuditAggregationResponse,
    AuditIntegrityResponse,
    AuditTransactionListResponse,
    AuditTransactionResponse,
)
from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/audit", tags=["audit"])


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Eagerly extract all fields from an ORM row into a plain dict.

    This prevents DetachedInstanceError when the session is closed
    before the response is serialized.
    """
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


def _dict_to_response(d: dict[str, Any]) -> AuditTransactionResponse:
    """Convert a plain dict to API response model."""
    return AuditTransactionResponse(**d)


def _build_filters(
    zone_id: str,
    *,
    protocol: str | None = None,
    buyer_agent_id: str | None = None,
    seller_agent_id: str | None = None,
    status: str | None = None,
    application: str | None = None,
    trace_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
) -> dict[str, Any]:
    """Build a filter dict, always including zone_id."""
    return {
        "zone_id": zone_id,
        "protocol": protocol,
        "buyer_agent_id": buyer_agent_id,
        "seller_agent_id": seller_agent_id,
        "status": status,
        "application": application,
        "trace_id": trace_id,
        "since": since,
        "until": until,
        "amount_min": amount_min,
        "amount_max": amount_max,
    }


# --------------------------------------------------------------------------
# List transactions
# --------------------------------------------------------------------------


@router.get("/transactions")
def list_transactions(
    since: datetime | None = Query(None, description="Transactions after this time (ISO-8601)"),
    until: datetime | None = Query(None, description="Transactions before this time (ISO-8601)"),
    protocol: str | None = Query(None, description="Filter by protocol"),
    buyer_agent_id: str | None = Query(None, description="Filter by buyer agent ID"),
    seller_agent_id: str | None = Query(None, description="Filter by seller agent ID"),
    status: str | None = Query(None, description="Filter by status"),
    application: str | None = Query(None, description="Filter by application"),
    trace_id: str | None = Query(None, description="Filter by trace ID"),
    amount_min: Decimal | None = Query(None, description="Minimum amount"),
    amount_max: Decimal | None = Query(None, description="Maximum amount"),
    limit: int = Query(100, ge=1, le=1000, description="Page size"),
    cursor: str | None = Query(None, description="Cursor from previous response"),
    include_total: bool = Query(False, description="Include total count"),
    logger_and_zone: tuple[ExchangeAuditLogger, str] = Depends(get_exchange_audit_logger),
) -> AuditTransactionListResponse:
    """List exchange audit transactions with cursor-based pagination."""
    audit_logger, zone_id = logger_and_zone

    filters = _build_filters(
        zone_id,
        protocol=protocol,
        buyer_agent_id=buyer_agent_id,
        seller_agent_id=seller_agent_id,
        status=status,
        application=application,
        trace_id=trace_id,
        since=since,
        until=until,
        amount_min=amount_min,
        amount_max=amount_max,
    )

    try:
        rows, next_cursor = audit_logger.list_transactions_cursor(
            filters=filters, limit=limit, cursor=cursor
        )
        total = None
        if include_total:
            total = audit_logger.count_transactions(**filters)

        return AuditTransactionListResponse(
            transactions=[_dict_to_response(_row_to_dict(r)) for r in rows],
            limit=limit,
            has_more=next_cursor is not None,
            total=total,
            next_cursor=next_cursor,
        )
    except Exception as e:
        logger.error("Audit query error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to query audit transactions") from e


# --------------------------------------------------------------------------
# Aggregations
# --------------------------------------------------------------------------


@router.get("/transactions/aggregations")
def get_aggregations(
    since: datetime | None = Query(None, description="Start time (ISO-8601)"),
    until: datetime | None = Query(None, description="End time (ISO-8601)"),
    logger_and_zone: tuple[ExchangeAuditLogger, str] = Depends(get_exchange_audit_logger),
) -> AuditAggregationResponse:
    """Compute aggregations: total volume, count, top counterparties."""
    audit_logger, zone_id = logger_and_zone

    try:
        agg = audit_logger.get_aggregations(zone_id=zone_id, since=since, until=until)
        return AuditAggregationResponse(**agg)
    except Exception as e:
        logger.error("Audit aggregation error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to compute aggregations") from e


# --------------------------------------------------------------------------
# Export (CSV / JSON)
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
    logger_and_zone: tuple[ExchangeAuditLogger, str] = Depends(get_exchange_audit_logger),
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

    # Eagerly extract to plain dicts to avoid DetachedInstanceError
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


# --------------------------------------------------------------------------
# Single transaction
# --------------------------------------------------------------------------


@router.get("/transactions/{record_id}")
def get_transaction(
    record_id: str,
    logger_and_zone: tuple[ExchangeAuditLogger, str] = Depends(get_exchange_audit_logger),
) -> AuditTransactionResponse:
    """Get a single audit transaction by ID."""
    audit_logger, zone_id = logger_and_zone

    row = audit_logger.get_transaction(record_id)
    if row is None or row.zone_id != zone_id:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _dict_to_response(_row_to_dict(row))


# --------------------------------------------------------------------------
# Integrity verification
# --------------------------------------------------------------------------


@router.get("/integrity/{record_id}")
def verify_integrity(
    record_id: str,
    logger_and_zone: tuple[ExchangeAuditLogger, str] = Depends(get_exchange_audit_logger),
) -> AuditIntegrityResponse:
    """Verify a record's hash matches its data (tamper detection)."""
    audit_logger, zone_id = logger_and_zone

    row = audit_logger.get_transaction(record_id)
    if row is None or row.zone_id != zone_id:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Eagerly cache before session closes
    record_hash = row.record_hash
    is_valid = audit_logger.verify_integrity_from_row(row)
    return AuditIntegrityResponse(
        record_id=record_id,
        is_valid=is_valid,
        record_hash=record_hash,
    )
