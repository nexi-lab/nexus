"""Secrets Audit RPC Service — secrets access logging.

Covers all secrets_audit.py endpoints except streaming export.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class SecretsAuditRPCService:
    """RPC surface for secrets audit trail operations."""

    def __init__(self, secrets_audit_logger: Any) -> None:
        self._logger = secrets_audit_logger

    @rpc_expose(description="List secrets audit events")
    def secrets_audit_list(
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
            filters["action"] = action
        if zone_id:
            filters["zone_id"] = zone_id
        result: dict[str, Any] = self._logger.list_events_cursor(
            filters=filters,
            limit=limit,
            cursor=cursor,
        )
        return result

    @rpc_expose(description="Get a single secrets audit event")
    def secrets_audit_get(self, record_id: str, zone_id: str | None = None) -> dict[str, Any]:
        row = self._logger.get_event(record_id)
        if row is None or (zone_id and getattr(row, "zone_id", None) != zone_id):
            return {"error": "Event not found"}
        return {
            "id": row.id,
            "action": row.action,
            "agent_id": row.agent_id,
            "zone_id": row.zone_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @rpc_expose(description="Verify secrets audit event integrity")
    def secrets_audit_integrity(self, record_id: str, zone_id: str | None = None) -> dict[str, Any]:
        row = self._logger.get_event(record_id)
        if row is None or (zone_id and getattr(row, "zone_id", None) != zone_id):
            return {"error": "Event not found"}
        is_valid = self._logger.verify_integrity(record_id)
        return {"record_id": record_id, "is_valid": is_valid}
