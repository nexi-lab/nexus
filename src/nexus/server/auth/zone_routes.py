"""Zone management API routes.

Provides endpoints for creating, updating, and managing zones.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus.server.auth.auth_routes import (
    get_auth_provider,
    get_authenticated_user,
    get_nexus_instance,
)
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.zone_helpers import (
    create_zone,
    normalize_to_slug,
    suggest_zone_id,
    validate_zone_id,
)
from nexus.server.auth.user_helpers import (
    add_user_to_zone,
    get_user_by_id,
    get_user_zones,
    user_belongs_to_zone,
)
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
    is_active: bool
    created_at: str
    updated_at: str


class ZoneListResponse(BaseModel):
    """List of zones."""

    zones: list[ZoneResponse]
    total: int


@router.post("", response_model=ZoneResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_endpoint(
    request: CreateZoneRequest,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> ZoneResponse:
    """Create a new zone.

    The authenticated user will be added as owner of the new zone.

    Args:
        request: Zone creation request
        user_info: Authenticated user (user_id, email) from JWT token
        auth: Authentication provider

    Returns:
        Created zone information

    Raises:
        400: Invalid zone_id or zone_id already taken
        401: Not authenticated
        500: Failed to assign creator as zone owner
    """
    user_id, _email = user_info

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
            if nx and hasattr(nx, "_rebac_manager"):
                try:
                    add_user_to_zone(
                        rebac_manager=nx._rebac_manager,
                        user_id=user_id,
                        zone_id=zone_id,
                        role="owner",
                        caller_user_id=None,  # System action, no caller check needed
                    )
                    logger.info(f"Added user {user_id} as owner of zone {zone_id}")
                except Exception as e:
                    logger.error(f"Failed to add user {user_id} as zone owner: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to assign creator as zone owner: {e}",
                    ) from e
            else:
                logger.warning(
                    f"NexusFS or ReBAC manager not available. "
                    f"User {user_id} not added as owner of zone {zone_id}"
                )

            return ZoneResponse(
                zone_id=zone.zone_id,
                name=zone.name,
                domain=zone.domain,
                description=zone.description,
                is_active=bool(zone.is_active),
                created_at=zone.created_at.isoformat(),
                updated_at=zone.updated_at.isoformat(),
            )

        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e


@router.get("/{zone_id}", response_model=ZoneResponse)
async def get_zone(
    zone_id: str,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> ZoneResponse:
    """Get zone information by ID.

    Only returns zone info if the authenticated user belongs to that zone
    or is a global admin.

    Args:
        zone_id: Zone identifier
        user_info: Authenticated user (user_id, email) from JWT token
        auth: Authentication provider

    Returns:
        Zone information

    Raises:
        401: Not authenticated
        403: User does not have access to this zone
        404: Zone not found
    """
    user_id, _email = user_info

    with auth.session_factory() as session:
        # Check if user is global admin
        user = get_user_by_id(session, user_id)
        is_global_admin = user and user.is_global_admin == 1

        # Check zone access (global admins can access any zone)
        if not is_global_admin:
            nx = get_nexus_instance()
            if nx and hasattr(nx, "_rebac_manager"):
                if not user_belongs_to_zone(nx._rebac_manager, user_id, zone_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Access denied: you are not a member of zone '{zone_id}'",
                    )
            else:
                # If ReBAC not available, deny access for non-admins
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: zone membership verification unavailable",
                )

        zone = session.get(ZoneModel, zone_id)
        if not zone:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Zone '{zone_id}' not found",
            )

        return ZoneResponse(
            zone_id=zone.zone_id,
            name=zone.name,
            domain=zone.domain,
            description=zone.description,
            is_active=bool(zone.is_active),
            created_at=zone.created_at.isoformat(),
            updated_at=zone.updated_at.isoformat(),
        )


@router.get("", response_model=ZoneListResponse)
async def list_zones(
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
    limit: int = 100,
    offset: int = 0,
) -> ZoneListResponse:
    """List zones the authenticated user belongs to.

    Global admins can see all zones. Regular users only see zones
    they are members of.

    Args:
        user_info: Authenticated user (user_id, email) from JWT token
        auth: Authentication provider
        limit: Maximum number of zones to return
        offset: Number of zones to skip

    Returns:
        List of zones

    Raises:
        401: Not authenticated
    """
    user_id, _email = user_info

    with auth.session_factory() as session:
        # Check if user is global admin
        user = get_user_by_id(session, user_id)
        is_global_admin = user and user.is_global_admin == 1

        if is_global_admin:
            # Global admins see all active zones
            stmt = (
                select(ZoneModel)
                .where(ZoneModel.is_active == 1)
                .order_by(ZoneModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            zones = session.scalars(stmt).all()

            # Count total active zones
            total_stmt = select(ZoneModel).where(ZoneModel.is_active == 1)
            total = len(session.scalars(total_stmt).all())
        else:
            # Regular users only see zones they belong to
            nx = get_nexus_instance()
            if nx and hasattr(nx, "_rebac_manager"):
                user_zone_ids = get_user_zones(nx._rebac_manager, user_id)
            else:
                user_zone_ids = []

            if not user_zone_ids:
                return ZoneListResponse(zones=[], total=0)

            # Query only zones user belongs to
            stmt = (
                select(ZoneModel)
                .where(
                    ZoneModel.is_active == 1,
                    ZoneModel.zone_id.in_(user_zone_ids),
                )
                .order_by(ZoneModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            zones = session.scalars(stmt).all()
            total = len(user_zone_ids)

        return ZoneListResponse(
            zones=[
                ZoneResponse(
                    zone_id=t.zone_id,
                    name=t.name,
                    domain=t.domain,
                    description=t.description,
                    is_active=bool(t.is_active),
                    created_at=t.created_at.isoformat(),
                    updated_at=t.updated_at.isoformat(),
                )
                for t in zones
            ],
            total=total,
        )
