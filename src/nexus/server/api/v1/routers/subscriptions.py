"""Subscription API router (Issue #1115, #1288).

Provides webhook subscription CRUD endpoints:
- POST   /api/subscriptions                      — create subscription
- GET    /api/subscriptions                      — list subscriptions
- GET    /api/subscriptions/{subscription_id}    — get subscription
- PATCH  /api/subscriptions/{subscription_id}    — update subscription
- DELETE /api/subscriptions/{subscription_id}    — delete subscription
- POST   /api/subscriptions/{subscription_id}/test — test subscription

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from nexus.server.api.v1.dependencies import get_subscription_manager
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["subscriptions"])


@router.post("/api/subscriptions", status_code=201)
async def create_subscription(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(get_subscription_manager),
) -> JSONResponse:
    """Create a new webhook subscription.

    Subscribe to file events (write, delete, rename) with optional path filters.
    """
    from nexus.server.subscriptions import SubscriptionCreate

    body = await request.json()
    data = SubscriptionCreate(**body)
    zone_id = auth_result.get("zone_id") or "default"
    created_by = auth_result.get("subject_id")

    subscription = subscription_manager.create(
        zone_id=zone_id,
        data=data,
        created_by=created_by,
    )
    return JSONResponse(content=subscription.model_dump(mode="json"), status_code=201)


@router.get("/api/subscriptions")
async def list_subscriptions(
    enabled_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(get_subscription_manager),
) -> JSONResponse:
    """List webhook subscriptions for the current zone."""
    zone_id = auth_result.get("zone_id") or "default"
    subscriptions = subscription_manager.list_subscriptions(
        zone_id=zone_id,
        enabled_only=enabled_only,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(
        content={"subscriptions": [s.model_dump(mode="json") for s in subscriptions]}
    )


@router.get("/api/subscriptions/{subscription_id}")
async def get_subscription(
    subscription_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(get_subscription_manager),
) -> JSONResponse:
    """Get a webhook subscription by ID."""
    zone_id = auth_result.get("zone_id") or "default"
    subscription = subscription_manager.get(subscription_id, zone_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return JSONResponse(content=subscription.model_dump(mode="json"))


@router.patch("/api/subscriptions/{subscription_id}")
async def update_subscription(
    subscription_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(get_subscription_manager),
) -> JSONResponse:
    """Update a webhook subscription."""
    from nexus.server.subscriptions import SubscriptionUpdate

    body = await request.json()
    data = SubscriptionUpdate(**body)
    zone_id = auth_result.get("zone_id") or "default"

    subscription = subscription_manager.update(
        subscription_id=subscription_id,
        zone_id=zone_id,
        data=data,
    )
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return JSONResponse(content=subscription.model_dump(mode="json"))


@router.delete("/api/subscriptions/{subscription_id}")
async def delete_subscription(
    subscription_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(get_subscription_manager),
) -> JSONResponse:
    """Delete a webhook subscription."""
    zone_id = auth_result.get("zone_id") or "default"
    deleted = subscription_manager.delete(subscription_id, zone_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return JSONResponse(content={"deleted": True})


@router.post("/api/subscriptions/{subscription_id}/test")
async def test_subscription(
    subscription_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(get_subscription_manager),
) -> JSONResponse:
    """Send a test event to a webhook subscription."""
    zone_id = auth_result.get("zone_id") or "default"
    result = await subscription_manager.test(subscription_id, zone_id)
    return JSONResponse(content=result)
