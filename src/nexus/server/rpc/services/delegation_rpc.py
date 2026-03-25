"""Delegation RPC Service — agent delegation lifecycle.

Issue #1271.
"""

import logging
from typing import Any, cast

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class DelegationRPCService:
    """RPC surface for agent delegation operations."""

    def __init__(self, delegation_service: Any) -> None:
        self._svc = delegation_service

    @rpc_expose(description="Create a delegation (spawn worker agent)")
    async def delegation_create(
        self,
        worker_id: str,
        worker_name: str = "",
        namespace_mode: str = "inherit",
        intent: str = "",
        ttl_seconds: int | None = None,
        can_sub_delegate: bool = False,
        auto_warmup: bool = True,
        owner_id: str = "",
        zone_id: str = "root",
    ) -> dict[str, Any]:
        result = await self._svc.create_delegation(
            worker_id=worker_id,
            worker_name=worker_name,
            namespace_mode=namespace_mode,
            intent=intent,
            ttl_seconds=ttl_seconds,
            can_sub_delegate=can_sub_delegate,
            auto_warmup=auto_warmup,
            owner_id=owner_id,
            zone_id=zone_id,
        )
        return {
            "delegation_id": result.delegation_id,
            "worker_id": result.worker_id,
            "api_key": result.api_key,
        }

    @rpc_expose(description="Revoke a delegation")
    async def delegation_revoke(self, delegation_id: str) -> dict[str, Any]:
        revoked = await self._svc.revoke_delegation(delegation_id)
        return {"revoked": revoked, "delegation_id": delegation_id}

    @rpc_expose(description="List delegations")
    async def delegation_list(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.list_delegations(
                limit=limit,
                offset=offset,
                status=status,
                agent_id=agent_id,
            ),
        )

    @rpc_expose(description="Get delegation chain")
    async def delegation_chain(self, delegation_id: str) -> dict[str, Any]:
        chain = await self._svc.get_delegation_chain(delegation_id)
        return {"delegation_id": delegation_id, "chain": chain}

    @rpc_expose(description="Complete a delegation")
    async def delegation_complete(
        self,
        delegation_id: str,
        outcome: str = "success",
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.complete_delegation(
                delegation_id,
                outcome=outcome,
                quality_score=quality_score,
            ),
        )
