"""Zone management API routes.

Provides endpoints for creating, updating, and managing zones.

Auth: Uses the unified ``require_auth`` dependency (supports JWT + API key +
static admin key) instead of the legacy JWT-only ``get_authenticated_user``.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
from nexus.bricks.auth.zone_helpers import (
    create_zone,
    normalize_to_slug,
    suggest_zone_id,
    validate_zone_id,
)
from nexus.lib.zone_helpers import (
    add_user_to_zone,
    get_user_zones,
    is_zone_owner,
    user_belongs_to_zone,
)
from nexus.server.auth.auth_routes import (
    get_auth_provider,
    get_nexus_instance,
)
from nexus.server.dependencies import require_auth
from nexus.storage.models import ZoneModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/zones", tags=["zones"])


# Request/Response Models
class CreateZoneRequest(BaseModel):
    """Request to create a new zone."""

    zone_id: str | None = Field(
        None,
        description="Desired zone ID (slug). If not provided, will be generated from name.",
        pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",
        min_length=3,
        max_length=63,
    )
    name: str = Field(..., description="Display name for the zone", min_length=1)
    domain: str | None = Field(None, description="Domain (e.g., company.com)")
    description: str | None = Field(None, description="Optional description")


class ZoneResponse(BaseModel):
    """Zone information response."""

    zone_id: str
    name: str
    domain: str | None = None
    description: str | None = None
    phase: str = "Active"
    finalizers: list[str] = []
    is_active: bool
    created_at: str
    updated_at: str
    limits: dict[str, Any] | None = None


class ZoneListResponse(BaseModel):
    """List of zones."""

    zones: list[ZoneResponse]
    total: int


def _zone_to_response(zone: ZoneModel) -> ZoneResponse:
    """Convert a ZoneModel to a ZoneResponse (DRY helper)."""
    # Extract limits from zone settings (forward-compatible via extra='allow')
    settings = zone.parsed_settings
    limits = getattr(settings, "limits", None)
    if limits is None:
        # Provide default quota stub so the field is always present
        limits = {
            "max_storage_bytes": 0,
            "max_files": 0,
            "max_agents": 0,
        }

    return ZoneResponse(
        zone_id=zone.zone_id,
        name=zone.name,
        domain=zone.domain,
        description=zone.description,
        phase=zone.phase,
        finalizers=zone.parsed_finalizers,
        is_active=zone.is_active,
        created_at=zone.created_at.isoformat(),
        updated_at=zone.updated_at.isoformat(),
        limits=limits,
    )


@router.post("", response_model=ZoneResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_endpoint(
    request: CreateZoneRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> ZoneResponse:
    """Create a new zone.

    The authenticated user will be added as owner of the new zone.

    Args:
        request: Zone creation request
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        auth: Authentication provider for DB session access

    Returns:
        Created zone information

    Raises:
        400: Invalid zone_id or zone_id already taken
        401: Not authenticated
        500: Failed to assign creator as zone owner
    """
    user_id = auth_result["subject_id"]

    with auth.session_factory() as session:
        # Determine zone_id
        if request.zone_id:
            # Validate provided zone_id
            is_valid, error_msg = validate_zone_id(request.zone_id)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_msg,
                )
            zone_id = request.zone_id
        else:
            # Generate zone_id from name
            suggested_slug = normalize_to_slug(request.name)
            zone_id = suggest_zone_id(suggested_slug, session)

        # Create zone
        try:
            zone = create_zone(
                session=session,
                zone_id=zone_id,
                name=request.name,
                domain=request.domain,
                description=request.description,
            )

            # Add authenticated user as zone owner via ReBAC
            nx = get_nexus_instance()
            # Issue #1771: rebac_manager via ServiceRegistry
            _rebac_mgr = nx.service("rebac_manager") if nx else None
            if nx and _rebac_mgr is not None:
                try:
                    add_user_to_zone(
                        rebac_manager=_rebac_mgr,
                        user_id=user_id,
                        zone_id=zone_id,
                        role="owner",
                        caller_user_id=None,  # System action, no caller check needed
                    )
                    logger.info("Added user %s as owner of zone %s", user_id, zone_id)
                except Exception as e:
                    logger.error(
                        "Failed to add user %s as zone owner: %s", user_id, e, exc_info=True
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to assign creator as zone owner: {e}",
                    ) from e
            else:
                logger.warning(
                    "NexusFS or ReBAC manager not available. User %s not added as owner of zone %s",
                    user_id,
                    zone_id,
                )

            return _zone_to_response(zone)

        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e


@router.get("/{zone_id}", response_model=ZoneResponse)
async def get_zone(
    zone_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> ZoneResponse:
    """Get zone information by ID.

    Only returns zone info if the authenticated user belongs to that zone
    or is a global admin.

    Args:
        zone_id: Zone identifier
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        auth: Authentication provider for DB session access

    Returns:
        Zone information

    Raises:
        401: Not authenticated
        403: User does not have access to this zone
        404: Zone not found
    """
    user_id = auth_result["subject_id"]
    is_admin = auth_result.get("is_admin", False)

    with auth.session_factory() as session:
        # Check zone access (admins can access any zone)
        nx = get_nexus_instance()
        rebac_mgr = getattr(nx, "_rebac_manager", None) if nx else None
        if not is_admin:
            if rebac_mgr is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot verify zone membership — ReBAC unavailable",
                )
            if not user_belongs_to_zone(rebac_mgr, user_id, zone_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: you are not a member of zone '{zone_id}'",
                )

        zone = session.get(ZoneModel, zone_id)
        if not zone:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Zone '{zone_id}' not found",
            )

        return _zone_to_response(zone)


def _get_session_factory(request: Request) -> Any:
    """Get a DB session factory from auth provider or NexusFS."""
    # Try auth provider first
    try:
        auth = get_auth_provider()
        return auth.session_factory
    except HTTPException:
        pass
    # Fallback: NexusFS.SessionLocal
    nx = getattr(request.app.state, "nexus_fs", None)
    sf = getattr(nx, "SessionLocal", None) if nx else None
    if sf is not None:
        return sf
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="No database session available for zone listing",
    )


@router.get("", response_model=ZoneListResponse)
async def list_zones(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    limit: int = 100,
    offset: int = 0,
) -> ZoneListResponse:
    """List zones the authenticated user belongs to.

    Global admins can see all zones. Regular users only see zones
    they are members of. Works with both DatabaseLocalAuth and
    API key authentication.

    Args:
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        limit: Maximum number of zones to return
        offset: Number of zones to skip

    Returns:
        List of zones

    Raises:
        401: Not authenticated
    """
    user_id = auth_result["subject_id"]
    is_admin = auth_result.get("is_admin", False)

    session_factory = _get_session_factory(request)
    with session_factory() as session:
        if is_admin:
            # Global admins see all active zones
            stmt = (
                select(ZoneModel)
                .where(ZoneModel.phase != "Terminated")
                .order_by(ZoneModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            zones = session.scalars(stmt).all()

            # Count total active zones (Issue #2070: use COUNT(*) not len())
            total = (
                session.scalar(
                    select(func.count())
                    .select_from(ZoneModel)
                    .where(ZoneModel.phase != "Terminated")
                )
                or 0
            )
        else:
            # Regular users only see zones they belong to
            nx = get_nexus_instance() or getattr(request.app.state, "nexus_fs", None)
            rebac_mgr = getattr(nx, "_rebac_manager", None) if nx else None
            # API-key auth may include zone_id — restrict to that zone
            auth_zone = auth_result.get("zone_id")
            user_zone_ids = (
                [auth_zone]
                if auth_zone
                else get_user_zones(rebac_mgr, user_id)
                if rebac_mgr
                else []
            )

            if not user_zone_ids:
                return ZoneListResponse(zones=[], total=0)

            # Query only zones user belongs to
            stmt = (
                select(ZoneModel)
                .where(
                    ZoneModel.phase != "Terminated",
                    ZoneModel.zone_id.in_(user_zone_ids),
                )
                .order_by(ZoneModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            zones = session.scalars(stmt).all()
            # Use actual DB count (user_zone_ids may include terminated zones)
            total = (
                session.scalar(
                    select(func.count())
                    .select_from(ZoneModel)
                    .where(
                        ZoneModel.phase != "Terminated",
                        ZoneModel.zone_id.in_(user_zone_ids),
                    )
                )
                or 0
            )

        return ZoneListResponse(
            zones=[_zone_to_response(t) for t in zones],
            total=total,
        )


class ZoneDeprovisionResponse(BaseModel):
    """Response for zone deprovision request."""

    zone_id: str
    phase: str
    finalizers_completed: list[str]
    finalizers_pending: list[str]
    finalizers_failed: dict[str, str]


@router.delete(
    "/{zone_id}",
    response_model=ZoneDeprovisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_zone_endpoint(
    zone_id: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> ZoneDeprovisionResponse:
    """Delete (deprovision) a zone.

    Initiates ordered zone teardown using the finalizer protocol.
    The zone enters ``Terminating`` phase, registered finalizers run
    cleanup, and the zone transitions to ``Terminated`` when complete.

    Idempotent: retrying on a ``Terminating`` zone retries pending finalizers.

    - Active → 202 Accepted (finalization started)
    - Terminating → 202 Accepted (retry pending finalizers)
    - Terminated → 404 Not Found

    Args:
        zone_id: Zone identifier
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        auth: Authentication provider for DB session access

    Raises:
        403: User is not zone owner or global admin
        404: Zone not found or already terminated
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    # Issue #3897: the default ROOT_ZONE_ID row is required by the
    # api_key_zones FK and by the startup bootstrap invariant
    # (nexus.storage.zone_bootstrap.ensure_root_zone). Deprovisioning it
    # would refuse server boot and break every root-scoped key creation.
    # Reject early so admin/owner UI flows surface a clear 403 instead
    # of a 500 from the lifecycle layer.
    if zone_id == ROOT_ZONE_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Zone {ROOT_ZONE_ID!r} is reserved and cannot be deleted",
        )

    user_id = auth_result["subject_id"]
    is_admin = auth_result.get("is_admin", False)

    # Get zone lifecycle service
    nx = get_nexus_instance()
    zone_lifecycle = getattr(nx, "_zone_lifecycle", None) if nx else None

    with auth.session_factory() as session:
        if not is_admin:
            # Require zone *owner* (not mere member) for destructive operations
            rebac_mgr = getattr(nx, "_rebac_manager", None) if nx else None
            if rebac_mgr is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot verify zone ownership — ReBAC unavailable",
                )
            if not is_zone_owner(rebac_mgr, user_id, zone_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: you are not the owner of zone '{zone_id}'",
                )

        zone = session.get(ZoneModel, zone_id)
        if not zone:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Zone '{zone_id}' not found",
            )

        # Enforce ownership for zone deletion (not just membership)
        _zone_owner: str | None = getattr(zone, "owner_id", None)
        if not is_admin and _zone_owner is not None and _zone_owner != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the zone owner can delete a zone",
            )

        if zone.phase == "Terminated":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Zone '{zone_id}' is already terminated",
            )

        if zone_lifecycle is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Zone lifecycle service is not available",
            )

        # Active → start finalization; Terminating → retry pending finalizers
        result = await zone_lifecycle.deprovision_zone(zone_id, session)

        return ZoneDeprovisionResponse(
            zone_id=result.zone_id,
            phase=result.phase,
            finalizers_completed=list(result.finalizers_completed),
            finalizers_pending=list(result.finalizers_pending),
            finalizers_failed=dict(result.finalizers_failed),
        )
