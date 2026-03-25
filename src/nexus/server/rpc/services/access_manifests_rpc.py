"""Access Manifests RPC Service — capability-based access control manifests.

Issue #1754.
"""

import logging
from typing import Any, cast

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class AccessManifestsRPCService:
    """RPC surface for access manifest operations."""

    def __init__(self, manifest_service: Any) -> None:
        self._svc = manifest_service

    @rpc_expose(description="Create an access manifest")
    async def access_manifests_create(
        self,
        agent_id: str,
        capabilities: list[dict[str, Any]],
        zone_id: str = "root",
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        result = await self._svc.create_manifest(
            agent_id=agent_id,
            capabilities=capabilities,
            zone_id=zone_id,
            ttl_seconds=ttl_seconds,
        )
        return {"manifest_id": result.manifest_id, "agent_id": agent_id}

    @rpc_expose(description="Get an access manifest by ID")
    async def access_manifests_get(self, manifest_id: str) -> dict[str, Any]:
        manifest = await self._svc.get_manifest(manifest_id)
        if manifest is None:
            return {"error": f"Manifest {manifest_id} not found"}
        return {
            "manifest_id": manifest.manifest_id,
            "agent_id": manifest.agent_id,
            "capabilities": manifest.capabilities,
            "active": manifest.active,
        }

    @rpc_expose(description="List access manifests")
    async def access_manifests_list(
        self,
        agent_id: str | None = None,
        zone_id: str | None = None,
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        manifests = await self._svc.list_manifests(
            agent_id=agent_id,
            zone_id=zone_id,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        return {
            "manifests": [
                {"manifest_id": m.manifest_id, "agent_id": m.agent_id, "active": m.active}
                for m in manifests
            ],
            "count": len(manifests),
        }

    @rpc_expose(description="Evaluate a tool against a manifest")
    async def access_manifests_evaluate(
        self, manifest_id: str, tool_name: str, resource: str | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.evaluate_tool(
                manifest_id,
                tool_name=tool_name,
                resource=resource,
            ),
        )

    @rpc_expose(description="Revoke an access manifest")
    async def access_manifests_revoke(self, manifest_id: str) -> dict[str, Any]:
        revoked = await self._svc.revoke_manifest(manifest_id)
        return {"revoked": revoked, "manifest_id": manifest_id}
