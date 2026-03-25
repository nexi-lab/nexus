"""Subscriptions RPC Service — webhook subscription management.

Issue #2056.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class SubscriptionsRPCService:
    """RPC surface for webhook subscription operations."""

    def __init__(self, subscription_manager: Any) -> None:
        self._manager = subscription_manager

    @rpc_expose(description="Create a webhook subscription")
    async def subscriptions_create(
        self,
        url: str,
        events: list[str],
        path_filter: str | None = None,
        zone_id: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        sub = await self._manager.create(
            url=url,
            events=events,
            path_filter=path_filter,
            zone_id=zone_id,
            secret=secret,
        )
        return {"subscription_id": sub.subscription_id, "url": sub.url}

    @rpc_expose(description="List webhook subscriptions")
    async def subscriptions_list(
        self,
        enabled_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        subs = await self._manager.list_subscriptions(
            enabled_only=enabled_only,
            limit=limit,
            offset=offset,
        )
        return {
            "subscriptions": [
                {"subscription_id": s.subscription_id, "url": s.url, "enabled": s.enabled}
                for s in subs
            ],
            "count": len(subs),
        }

    @rpc_expose(description="Get subscription details")
    async def subscriptions_get(self, subscription_id: str) -> dict[str, Any]:
        sub = await self._manager.get(subscription_id)
        if sub is None:
            return {"error": f"Subscription {subscription_id} not found"}
        return {
            "subscription_id": sub.subscription_id,
            "url": sub.url,
            "events": sub.events,
            "enabled": sub.enabled,
        }

    @rpc_expose(description="Update a subscription")
    async def subscriptions_update(self, subscription_id: str, **kwargs: Any) -> dict[str, Any]:
        sub = await self._manager.update(subscription_id, **kwargs)
        if sub is None:
            return {"error": f"Subscription {subscription_id} not found"}
        return {"subscription_id": sub.subscription_id, "updated": True}

    @rpc_expose(description="Delete a subscription")
    async def subscriptions_delete(self, subscription_id: str) -> dict[str, Any]:
        deleted = await self._manager.delete(subscription_id)
        return {"deleted": deleted, "subscription_id": subscription_id}

    @rpc_expose(description="Test a subscription with a sample event")
    async def subscriptions_test(self, subscription_id: str) -> dict[str, Any]:
        result = await self._manager.test(subscription_id)
        return {"subscription_id": subscription_id, "success": result}
