"""Audit RPC Service — transaction listing and export.

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
        result: dict[str, Any] = self._audit_logger.list_transactions_cursor(
            filters=filters, limit=limit, cursor=cursor
        )
        return result

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
