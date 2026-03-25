"""Aspects RPC Service — metadata aspect CRUD.

Issue #2930.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class AspectsRPCService:
    """RPC surface for aspect metadata operations."""

    def __init__(self, aspect_service: Any) -> None:
        self._aspect_svc = aspect_service

    @rpc_expose(description="List aspects for an entity URN")
    async def aspects_list(self, urn: str, zone_id: str = "root") -> dict[str, Any]:
        aspects = await self._aspect_svc.list_aspects(urn, zone_id=zone_id)
        return {
            "urn": urn,
            "aspects": [
                {"name": a.name, "version": a.version, "updated_at": str(a.updated_at)}
                for a in aspects
            ],
        }

    @rpc_expose(description="Get a specific aspect")
    async def aspects_get(self, urn: str, name: str, version: int | None = None) -> dict[str, Any]:
        aspect = await self._aspect_svc.get_aspect(urn, name, version=version)
        if aspect is None:
            return {"error": f"Aspect {name} not found for {urn}"}
        return {
            "urn": urn,
            "name": aspect.name,
            "version": aspect.version,
            "data": aspect.data,
        }

    @rpc_expose(description="Get aspect version history")
    async def aspects_history(self, urn: str, name: str, limit: int = 10) -> dict[str, Any]:
        history = await self._aspect_svc.get_aspect_history(urn, name, limit=limit)
        return {
            "urn": urn,
            "name": name,
            "versions": [{"version": h.version, "updated_at": str(h.updated_at)} for h in history],
        }

    @rpc_expose(description="Create or update an aspect")
    async def aspects_put(
        self, urn: str, name: str, data: dict[str, Any], zone_id: str = "root"
    ) -> dict[str, Any]:
        result = await self._aspect_svc.put_aspect(urn, name, data, zone_id=zone_id)
        return {"urn": urn, "name": name, "version": result.version}

    @rpc_expose(description="Delete an aspect")
    async def aspects_delete(self, urn: str, name: str) -> dict[str, Any]:
        deleted = await self._aspect_svc.delete_aspect(urn, name)
        return {"deleted": deleted, "urn": urn, "name": name}
