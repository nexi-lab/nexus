"""Aspects REST API endpoints (Issue #2930).

Provides endpoints for managing entity aspects:
- GET /api/v2/aspects/{urn} -- List all aspects for an entity
- GET /api/v2/aspects/{urn}/{name} -- Get a specific aspect
- PUT /api/v2/aspects/{urn}/{name} -- Create or update an aspect
- DELETE /api/v2/aspects/{urn}/{name} -- Delete an aspect
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.dependencies import get_aspect_service
from nexus.server.api.v2.models.aspects import (
    AspectHistoryResponse,
    AspectListResponse,
    AspectResponse,
    PutAspectRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/aspects", tags=["aspects"])


def _verify_urn_zone(urn: str, zone_id: str) -> None:
    """Verify URN belongs to caller's zone (prevent cross-zone data exposure).

    URNs are one-way hashes so we check the zone component embedded in the URN.
    Root zone bypasses the check as it has global visibility.
    """
    if zone_id == ROOT_ZONE_ID:
        return
    if f":{zone_id}:" not in urn:
        raise HTTPException(status_code=403, detail="Access denied: URN is outside your zone")


@router.get("/{urn}")
async def list_aspects(
    urn: str = Path(..., description="Entity URN"),
    aspect_and_zone: tuple[Any, str] = Depends(get_aspect_service),
) -> AspectListResponse:
    """List all aspect names attached to an entity."""
    aspect_svc, _zone_id = aspect_and_zone
    _verify_urn_zone(urn, _zone_id)
    try:
        names = aspect_svc.list_aspects(urn)
        return AspectListResponse(entity_urn=urn, aspects=names)
    except Exception as e:
        logger.error("list_aspects error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list aspects") from e


@router.get("/{urn}/{name}")
async def get_aspect(
    urn: str = Path(..., description="Entity URN"),
    name: str = Path(..., description="Aspect name"),
    version: int | None = Query(None, description="Specific version (default: current)"),
    aspect_and_zone: tuple[Any, str] = Depends(get_aspect_service),
) -> AspectResponse:
    """Get a specific aspect for an entity."""
    aspect_svc, _zone_id = aspect_and_zone
    _verify_urn_zone(urn, _zone_id)
    try:
        if version is not None:
            payload = aspect_svc.get_aspect_version(urn, name, version)
        else:
            payload = aspect_svc.get_aspect(urn, name)

        if payload is None:
            raise HTTPException(status_code=404, detail=f"Aspect '{name}' not found for {urn}")

        return AspectResponse(
            entity_urn=urn,
            aspect_name=name,
            version=version or 0,
            payload=payload,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_aspect error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get aspect") from e


@router.get("/{urn}/{name}/history")
async def get_aspect_history(
    urn: str = Path(..., description="Entity URN"),
    name: str = Path(..., description="Aspect name"),
    limit: int = Query(20, ge=1, le=100, description="Max versions to return"),
    aspect_and_zone: tuple[Any, str] = Depends(get_aspect_service),
) -> AspectHistoryResponse:
    """Get version history for a specific aspect."""
    aspect_svc, _zone_id = aspect_and_zone
    _verify_urn_zone(urn, _zone_id)
    try:
        history = aspect_svc.get_aspect_history(urn, name, limit=limit)
        versions = [
            AspectResponse(
                entity_urn=urn,
                aspect_name=name,
                version=h["version"],
                payload=h["payload"],
                created_by=h.get("created_by", "system"),
                created_at=h.get("created_at"),
            )
            for h in history
        ]
        return AspectHistoryResponse(entity_urn=urn, aspect_name=name, versions=versions)
    except Exception as e:
        logger.error("get_aspect_history error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get aspect history") from e


@router.put("/{urn}/{name}")
async def put_aspect(
    body: PutAspectRequest,
    urn: str = Path(..., description="Entity URN"),
    name: str = Path(..., description="Aspect name"),
    aspect_and_zone: tuple[Any, str] = Depends(get_aspect_service),
) -> AspectResponse:
    """Create or update an aspect."""
    aspect_svc, zone_id = aspect_and_zone
    _verify_urn_zone(urn, zone_id)  # Prevent cross-zone mutation
    try:
        aspect_svc.put_aspect(
            entity_urn=urn,
            aspect_name=name,
            payload=body.payload,
            created_by=body.created_by,
            zone_id=zone_id,
        )
        # Return the newly written aspect
        payload = aspect_svc.get_aspect(urn, name)
        return AspectResponse(
            entity_urn=urn,
            aspect_name=name,
            version=0,
            payload=payload or body.payload,
            created_by=body.created_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.error("put_aspect error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to put aspect") from e


@router.delete("/{urn}/{name}", status_code=204)
async def delete_aspect(
    urn: str = Path(..., description="Entity URN"),
    name: str = Path(..., description="Aspect name"),
    aspect_and_zone: tuple[Any, str] = Depends(get_aspect_service),
) -> None:
    """Delete an aspect."""
    aspect_svc, zone_id = aspect_and_zone
    _verify_urn_zone(urn, zone_id)  # Prevent cross-zone mutation
    try:
        deleted = aspect_svc.delete_aspect(urn, name, zone_id=zone_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Aspect '{name}' not found for {urn}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_aspect error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete aspect") from e
