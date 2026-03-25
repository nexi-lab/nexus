"""Conflicts RPC Service — sync conflict management.

Issue #1130.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class ConflictsRPCService:
    """RPC surface for sync conflict operations."""

    def __init__(self, conflict_log_store: Any) -> None:
        self._store = conflict_log_store

    @rpc_expose(description="List sync conflicts", admin_only=True)
    async def conflicts_list(
        self,
        status: str | None = None,
        backend_name: str | None = None,
        zone_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        conflicts = self._store.list_conflicts(
            status=status,
            backend_name=backend_name,
            zone_id=zone_id,
            limit=limit,
            offset=offset,
        )
        return {
            "conflicts": [
                {
                    "conflict_id": c.conflict_id,
                    "path": c.path,
                    "status": c.status,
                    "backend_name": c.backend_name,
                    "zone_id": c.zone_id,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in conflicts
            ],
            "count": len(conflicts),
        }

    @rpc_expose(description="Get conflict details", admin_only=True)
    async def conflicts_get(self, conflict_id: str) -> dict[str, Any]:
        conflict = self._store.get_conflict(conflict_id)
        if conflict is None:
            return {"error": f"Conflict {conflict_id} not found"}
        return {
            "conflict_id": conflict.conflict_id,
            "path": conflict.path,
            "status": conflict.status,
            "backend_name": conflict.backend_name,
            "zone_id": conflict.zone_id,
            "local_hash": conflict.local_hash,
            "remote_hash": conflict.remote_hash,
            "created_at": conflict.created_at.isoformat() if conflict.created_at else None,
        }

    @rpc_expose(description="Resolve a sync conflict", admin_only=True)
    async def conflicts_resolve(
        self, conflict_id: str, outcome: str = "accept_local"
    ) -> dict[str, Any]:
        resolved = self._store.resolve_conflict(conflict_id, outcome=outcome)
        if not resolved:
            return {"error": f"Conflict {conflict_id} not found or already resolved"}
        return {"resolved": True, "conflict_id": conflict_id, "outcome": outcome}
