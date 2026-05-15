"""Subscription API v2 router (#2056).

Provides webhook subscription CRUD endpoints:
- POST   /api/v2/subscriptions                      — create subscription
- GET    /api/v2/subscriptions                      — list subscriptions
- GET    /api/v2/subscriptions/{subscription_id}    — get subscription
- PATCH  /api/v2/subscriptions/{subscription_id}    — update subscription
- DELETE /api/v2/subscriptions/{subscription_id}    — delete subscription
- POST   /api/v2/subscriptions/{subscription_id}/test — test subscription

Ported from v1 with improvements:
- Pydantic request models replace raw request.json()
- Proper response models
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_auth
from nexus.server.zone_execution import run_zone_scoped

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/subscriptions", tags=["subscriptions"])

# =============================================================================
# Dependencies
# =============================================================================


def _get_subscription_manager(request: Request) -> Any:
    """Get SubscriptionManager from app.state, raising 503 if not available."""
    mgr = getattr(request.app.state, "subscription_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Subscription manager not available")
    return mgr


def _get_zone_registry(request: Request) -> Any | None:
    return getattr(request.app.state, "zone_registry", None)


def _auth_zone(auth_result: dict[str, Any]) -> str | None:
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    return zone_id if zone_id != ROOT_ZONE_ID else None


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", status_code=201)
async def create_subscription(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(_get_subscription_manager),
) -> JSONResponse:
    """Create a new webhook subscription.

    Subscribe to file events (write, delete, rename) with optional path filters.
    """
    from nexus.server.subscriptions import SubscriptionCreate

    body = await request.json()
    data = SubscriptionCreate(**body)
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    created_by = auth_result.get("subject_id")

    async def _create() -> JSONResponse:
        subscription = subscription_manager.create(
            zone_id=zone_id,
            data=data,
            created_by=created_by,
        )
        return JSONResponse(content=subscription.model_dump(mode="json"), status_code=201)

    return await run_zone_scoped(_get_zone_registry(request), _auth_zone(auth_result), _create)


@router.get("")
async def list_subscriptions(
    request: Request,
    enabled_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(_get_subscription_manager),
) -> JSONResponse:
    """List webhook subscriptions for the current zone."""
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    async def _list() -> JSONResponse:
        subscriptions = subscription_manager.list_subscriptions(
            zone_id=zone_id,
            enabled_only=enabled_only,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            content={"subscriptions": [s.model_dump(mode="json") for s in subscriptions]}
        )

    return await run_zone_scoped(_get_zone_registry(request), _auth_zone(auth_result), _list)


@router.get("/{subscription_id}")
async def get_subscription(
    subscription_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(_get_subscription_manager),
) -> JSONResponse:
    """Get a webhook subscription by ID."""
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    async def _get() -> JSONResponse:
        subscription = subscription_manager.get(subscription_id, zone_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return JSONResponse(content=subscription.model_dump(mode="json"))

    return await run_zone_scoped(_get_zone_registry(request), _auth_zone(auth_result), _get)


@router.patch("/{subscription_id}")
async def update_subscription(
    subscription_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(_get_subscription_manager),
) -> JSONResponse:
    """Update a webhook subscription."""
    from nexus.server.subscriptions import SubscriptionUpdate

    body = await request.json()
    data = SubscriptionUpdate(**body)
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    async def _update() -> JSONResponse:
        subscription = subscription_manager.update(
            subscription_id=subscription_id,
            zone_id=zone_id,
            data=data,
        )
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return JSONResponse(content=subscription.model_dump(mode="json"))

    return await run_zone_scoped(_get_zone_registry(request), _auth_zone(auth_result), _update)


@router.delete("/{subscription_id}")
async def delete_subscription(
    subscription_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(_get_subscription_manager),
) -> JSONResponse:
    """Delete a webhook subscription."""
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    async def _delete() -> JSONResponse:
        deleted = subscription_manager.delete(subscription_id, zone_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return JSONResponse(content={"deleted": True})

    return await run_zone_scoped(_get_zone_registry(request), _auth_zone(auth_result), _delete)


@router.post("/{subscription_id}/test")
async def test_subscription(
    subscription_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    subscription_manager: Any = Depends(_get_subscription_manager),
) -> JSONResponse:
    """Send a test event to a webhook subscription."""
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    async def _test() -> JSONResponse:
        result = await subscription_manager.test(subscription_id, zone_id)
        return JSONResponse(content=result)

    return await run_zone_scoped(_get_zone_registry(request), _auth_zone(auth_result), _test)
