"""Federation API v2 router (A4: server-side federation endpoints).

Exposes ZoneManager and NexusFederation operations via HTTP:

Raft-level zone operations (admin-only):
- GET    /api/v2/federation/zones                      — list Raft zones
- GET    /api/v2/federation/zones/{zone_id}/cluster-info — cluster info
- POST   /api/v2/federation/zones                      — create a zone
- DELETE /api/v2/federation/zones/{zone_id}             — remove a zone

Mount operations (admin-only):
- POST   /api/v2/federation/mounts  — mount zone
- DELETE /api/v2/federation/mounts  — unmount zone

Share/Join operations (authenticated users):
- POST   /api/v2/federation/share   — share a subtree
- POST   /api/v2/federation/join    — join a peer's zone
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from nexus.server.dependencies import require_admin, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/federation", tags=["federation"])


# =============================================================================
# Request/Response models
# =============================================================================


class ZoneSummary(BaseModel):
    """Summary of a single Raft zone."""

    zone_id: str = Field(description="Unique zone identifier")
    links_count: int = Field(description="Number of mount references (i_links_count)")


class ZoneListResponse(BaseModel):
    """Response for listing all Raft zones."""

    zones: list[ZoneSummary] = Field(description="List of zones with their link counts")


class ClusterInfoResponse(BaseModel):
    """Cluster info for a specific zone."""

    zone_id: str = Field(description="Zone identifier")
    node_id: int = Field(description="This node's Raft node ID")
    links_count: int = Field(description="Number of mount references (i_links_count)")
    has_store: bool = Field(description="Whether a RaftMetadataStore exists for this zone")


class CreateZoneRequest(BaseModel):
    """Request body for creating a new Raft zone."""

    zone_id: str = Field(description="Unique zone identifier")
    peers: list[str] | None = Field(
        default=None, description='Peer addresses in "id@host:port" format'
    )


class CreateZoneResponse(BaseModel):
    """Response after creating a zone."""

    zone_id: str = Field(description="Created zone identifier")
    created: bool = Field(default=True, description="Whether the zone was created")


class RemoveZoneResponse(BaseModel):
    """Response after removing a zone."""

    zone_id: str = Field(description="Removed zone identifier")
    removed: bool = Field(default=True, description="Whether the zone was removed")


class MountRequest(BaseModel):
    """Request body for mounting a zone."""

    parent_zone_id: str = Field(description="Zone containing the mount point")
    mount_path: str = Field(description="Path in parent zone where target is mounted")
    target_zone_id: str = Field(description="Zone to mount at the path")


class MountResponse(BaseModel):
    """Response after mounting a zone."""

    parent_zone_id: str = Field(description="Zone containing the mount point")
    mount_path: str = Field(description="Path where the zone was mounted")
    target_zone_id: str = Field(description="Zone that was mounted")
    mounted: bool = Field(default=True, description="Whether the mount succeeded")


class UnmountRequest(BaseModel):
    """Request body for unmounting a zone."""

    parent_zone_id: str = Field(description="Zone containing the mount point")
    mount_path: str = Field(description="Path to unmount")


class UnmountResponse(BaseModel):
    """Response after unmounting a zone."""

    parent_zone_id: str = Field(description="Zone containing the mount point")
    mount_path: str = Field(description="Path that was unmounted")
    unmounted: bool = Field(default=True, description="Whether the unmount succeeded")


class ShareRequest(BaseModel):
    """Request body for sharing a subtree."""

    local_path: str = Field(description="Local path to share (e.g., /usr/alice/projectA)")
    zone_id: str | None = Field(
        default=None, description="Explicit zone ID (auto-generated UUID if omitted)"
    )


class ShareResponse(BaseModel):
    """Response after sharing a subtree."""

    zone_id: str = Field(description="Zone ID of the shared subtree")
    local_path: str = Field(description="Local path that was shared")
    shared: bool = Field(default=True, description="Whether the share succeeded")


class JoinRequest(BaseModel):
    """Request body for joining a peer's zone."""

    peer_addr: str = Field(description="Peer's gRPC address (e.g., bob:2126)")
    remote_path: str = Field(description="Path on peer to join (e.g., /shared-projectA)")
    local_path: str = Field(description="Local mount point (e.g., /usr/charlie/shared)")


class JoinResponse(BaseModel):
    """Response after joining a peer's zone."""

    zone_id: str = Field(description="Zone ID that was joined")
    peer_addr: str = Field(description="Peer address that was contacted")
    local_path: str = Field(description="Local mount point")
    joined: bool = Field(default=True, description="Whether the join succeeded")


# =============================================================================
# Dependencies
# =============================================================================


def _get_zone_manager(request: Request) -> Any:
    """Get the ZoneManager from app state, raising 503 if not available."""
    mgr = getattr(request.app.state, "zone_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Federation not available")
    return mgr


def _get_federation(request: Request) -> Any | None:
    """Get the NexusFederation from app state, or None if not configured."""
    return getattr(request.app.state, "federation", None)


# =============================================================================
# Raft-level zone operations (admin-only)
# =============================================================================


@router.get("/zones", response_model=ZoneListResponse)
async def list_zones(
    _auth: dict[str, Any] = Depends(require_admin),
    zone_manager: Any = Depends(_get_zone_manager),
) -> ZoneListResponse:
    """List all Raft zones managed by this node."""
    zone_ids: list[str] = zone_manager.list_zones()
    zones = [
        ZoneSummary(
            zone_id=zid,
            links_count=zone_manager.get_links_count(zid),
        )
        for zid in zone_ids
    ]
    return ZoneListResponse(zones=zones)


@router.get("/zones/{zone_id}/cluster-info", response_model=ClusterInfoResponse)
async def get_cluster_info(
    zone_id: str,
    _auth: dict[str, Any] = Depends(require_admin),
    zone_manager: Any = Depends(_get_zone_manager),
) -> ClusterInfoResponse:
    """Get cluster info for a specific zone."""
    store = zone_manager.get_store(zone_id)
    return ClusterInfoResponse(
        zone_id=zone_id,
        node_id=zone_manager.node_id,
        links_count=zone_manager.get_links_count(zone_id),
        has_store=store is not None,
    )


@router.post("/zones", status_code=201, response_model=CreateZoneResponse)
async def create_zone(
    request: CreateZoneRequest,
    _auth: dict[str, Any] = Depends(require_admin),
    zone_manager: Any = Depends(_get_zone_manager),
) -> CreateZoneResponse:
    """Create a new Raft zone."""
    try:
        zone_manager.create_zone(request.zone_id, peers=request.peers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info("Zone '%s' created via API", request.zone_id)
    return CreateZoneResponse(zone_id=request.zone_id, created=True)


@router.delete("/zones/{zone_id}", response_model=RemoveZoneResponse)
async def remove_zone(
    zone_id: str,
    force: bool = Query(False, description="Force removal even if zone has references"),
    _auth: dict[str, Any] = Depends(require_admin),
    zone_manager: Any = Depends(_get_zone_manager),
) -> RemoveZoneResponse:
    """Remove a Raft zone, shutting down its Raft group."""
    try:
        zone_manager.remove_zone(zone_id, force=force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info("Zone '%s' removed via API (force=%s)", zone_id, force)
    return RemoveZoneResponse(zone_id=zone_id, removed=True)


# =============================================================================
# Mount operations (admin-only)
# =============================================================================


@router.post("/mounts", status_code=201, response_model=MountResponse)
async def mount_zone(
    request: MountRequest,
    _auth: dict[str, Any] = Depends(require_admin),
    zone_manager: Any = Depends(_get_zone_manager),
) -> MountResponse:
    """Mount a zone at a path in another zone."""
    try:
        zone_manager.mount(
            request.parent_zone_id,
            request.mount_path,
            request.target_zone_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    logger.info(
        "Zone '%s' mounted at '%s' in zone '%s' via API",
        request.target_zone_id,
        request.mount_path,
        request.parent_zone_id,
    )
    return MountResponse(
        parent_zone_id=request.parent_zone_id,
        mount_path=request.mount_path,
        target_zone_id=request.target_zone_id,
        mounted=True,
    )


@router.delete("/mounts", response_model=UnmountResponse)
async def unmount_zone(
    request: UnmountRequest,
    _auth: dict[str, Any] = Depends(require_admin),
    zone_manager: Any = Depends(_get_zone_manager),
) -> UnmountResponse:
    """Unmount a zone, restoring the original directory."""
    try:
        zone_manager.unmount(request.parent_zone_id, request.mount_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    logger.info(
        "Unmounted '%s' from zone '%s' via API",
        request.mount_path,
        request.parent_zone_id,
    )
    return UnmountResponse(
        parent_zone_id=request.parent_zone_id,
        mount_path=request.mount_path,
        unmounted=True,
    )


# =============================================================================
# Share/Join operations (authenticated users)
# =============================================================================


@router.post("/share", status_code=201, response_model=ShareResponse)
async def share_subtree(
    request: ShareRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    federation: Any | None = Depends(_get_federation),
) -> ShareResponse:
    """Share a local subtree by creating a new zone.

    Uses NexusFederation.share() which delegates to
    ZoneManager.share_subtree() under the hood.
    """
    if federation is None:
        raise HTTPException(status_code=503, detail="Federation not configured")

    try:
        zone_id: str = await federation.share(
            local_path=request.local_path,
            zone_id=request.zone_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info("Shared '%s' as zone '%s' via API", request.local_path, zone_id)
    return ShareResponse(
        zone_id=zone_id,
        local_path=request.local_path,
        shared=True,
    )


@router.post("/join", status_code=201, response_model=JoinResponse)
async def join_zone(
    request: JoinRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    federation: Any | None = Depends(_get_federation),
) -> JoinResponse:
    """Join a peer's shared zone via federation protocol.

    Uses NexusFederation.join() which discovers the zone via gRPC,
    creates a local Raft replica, and mounts it locally.
    """
    if federation is None:
        raise HTTPException(status_code=503, detail="Federation not configured")

    try:
        zone_id: str = await federation.join(
            peer_addr=request.peer_addr,
            remote_path=request.remote_path,
            local_path=request.local_path,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info(
        "Joined zone '%s' from %s via API, mounted at '%s'",
        zone_id,
        request.peer_addr,
        request.local_path,
    )
    return JoinResponse(
        zone_id=zone_id,
        peer_addr=request.peer_addr,
        local_path=request.local_path,
        joined=True,
    )
