"""Audit RPC Service — transaction listing, export, aggregations, integrity.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class AuditRPCService:
    """RPC surface for exchange audit trail."""

    def __init__(self, audit_logger: Any) -> None:
        self._audit_logger = audit_logger

    @rpc_expose(description="List audit trail entries")
    def audit_list(
        self,
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        action: str | None = None,
        zone_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if since:
            filters["since"] = since
        if until:
            filters["until"] = until
        if agent_id:
            filters["agent_id"] = agent_id
        if action:
            filters["status"] = action
        if zone_id:
            filters["zone_id"] = zone_id
        result: dict[str, Any] = self._audit_logger.list_transactions_cursor(
            filters=filters, limit=limit, cursor=cursor
        )
        return result

    @rpc_expose(description="Get a single audit transaction by ID")
    def audit_get(self, record_id: str, zone_id: str | None = None) -> dict[str, Any]:
        row = self._audit_logger.get_transaction(record_id)
        if row is None or (zone_id and row.zone_id != zone_id):
            return {"error": "Transaction not found"}
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
            "transfer_id": row.transfer_id,
        }

    @rpc_expose(description="Export audit data")
    def audit_export(
        self,
        fmt: str = "json",
        since: str | None = None,
        until: str | None = None,
    ) -> str:
        import csv
        import io
        import json

        filters: dict[str, Any] = {}
        if since:
            filters["since"] = since
        if until:
            filters["until"] = until

        rows = list(self._audit_logger.iter_transactions(filters))
        # Convert ORM objects to plain dicts
        records = []
        for row in rows:
            d = {c.name: getattr(row, c.name, None) for c in row.__table__.columns}
            records.append({k: str(v) if v is not None else None for k, v in d.items()})

        if fmt == "csv":
            if not records:
                return ""
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
            return buf.getvalue()
        return json.dumps(records, indent=2, default=str)

    @rpc_expose(description="Compute audit aggregations (volume, count, top counterparties)")
    def audit_aggregations(
        self,
        zone_id: str = "root",
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        from datetime import datetime

        since_dt = datetime.fromisoformat(since) if since else None
        until_dt = datetime.fromisoformat(until) if until else None
        result: dict[str, Any] = self._audit_logger.get_aggregations(
            zone_id=zone_id, since=since_dt, until=until_dt
        )
        return result

    @rpc_expose(description="Verify audit record integrity (tamper detection)")
    def audit_integrity(self, record_id: str, zone_id: str | None = None) -> dict[str, Any]:
        row = self._audit_logger.get_transaction(record_id)
        if row is None or (zone_id and row.zone_id != zone_id):
            return {"error": "Transaction not found"}
        record_hash = row.record_hash
        is_valid = self._audit_logger.verify_integrity_from_row(row)
        return {
            "record_id": record_id,
            "is_valid": is_valid,
            "record_hash": record_hash,
        }
