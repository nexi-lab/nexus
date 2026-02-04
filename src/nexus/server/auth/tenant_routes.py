"""Tenant management API routes.

Provides endpoints for creating, updating, and managing tenants.
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
from nexus.server.auth.tenant_helpers import (
    create_tenant,
    normalize_to_slug,
    suggest_tenant_id,
    validate_tenant_id,
)
from nexus.server.auth.user_helpers import (
    add_user_to_tenant,
    get_user_by_id,
    get_user_tenants,
    user_belongs_to_tenant,
)
from nexus.storage.models import TenantModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


# Request/Response Models
class CreateTenantRequest(BaseModel):
    """Request to create a new tenant."""

    tenant_id: str | None = Field(
        None,
        description="Desired tenant ID (slug). If not provided, will be generated from name.",
        pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",
        min_length=3,
        max_length=63,
    )
    name: str = Field(..., description="Display name for the tenant", min_length=1)
    domain: str | None = Field(None, description="Domain (e.g., company.com)")
    description: str | None = Field(None, description="Optional description")


class TenantResponse(BaseModel):
    """Tenant information response."""

    tenant_id: str
    name: str
    domain: str | None = None
    description: str | None = None
    is_active: bool
    created_at: str
    updated_at: str


class TenantListResponse(BaseModel):
    """List of tenants."""

    tenants: list[TenantResponse]
    total: int


@router.post("", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant_endpoint(
    request: CreateTenantRequest,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> TenantResponse:
    """Create a new tenant.

    The authenticated user will be added as owner of the new tenant.

    Args:
        request: Tenant creation request
        user_info: Authenticated user (user_id, email) from JWT token
        auth: Authentication provider

    Returns:
        Created tenant information

    Raises:
        400: Invalid tenant_id or tenant_id already taken
        401: Not authenticated
        500: Failed to assign creator as tenant owner
    """
    user_id, _email = user_info

    with auth.session_factory() as session:
        # Determine tenant_id
        if request.tenant_id:
            # Validate provided tenant_id
            is_valid, error_msg = validate_tenant_id(request.tenant_id)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_msg,
                )
            tenant_id = request.tenant_id
        else:
            # Generate tenant_id from name
            suggested_slug = normalize_to_slug(request.name)
            tenant_id = suggest_tenant_id(suggested_slug, session)

        # Create tenant
        try:
            tenant = create_tenant(
                session=session,
                tenant_id=tenant_id,
                name=request.name,
                domain=request.domain,
                description=request.description,
            )

            # Add authenticated user as tenant owner via ReBAC
            nx = get_nexus_instance()
            if nx and hasattr(nx, "_rebac_manager"):
                try:
                    add_user_to_tenant(
                        rebac_manager=nx._rebac_manager,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        role="owner",
                        caller_user_id=None,  # System action, no caller check needed
                    )
                    logger.info(f"Added user {user_id} as owner of tenant {tenant_id}")
                except Exception as e:
                    logger.error(f"Failed to add user {user_id} as tenant owner: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to assign creator as tenant owner: {e}",
                    ) from e
            else:
                logger.warning(
                    f"NexusFS or ReBAC manager not available. "
                    f"User {user_id} not added as owner of tenant {tenant_id}"
                )

            return TenantResponse(
                tenant_id=tenant.tenant_id,
                name=tenant.name,
                domain=tenant.domain,
                description=tenant.description,
                is_active=bool(tenant.is_active),
                created_at=tenant.created_at.isoformat(),
                updated_at=tenant.updated_at.isoformat(),
            )

        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> TenantResponse:
    """Get tenant information by ID.

    Only returns tenant info if the authenticated user belongs to that tenant
    or is a global admin.

    Args:
        tenant_id: Tenant identifier
        user_info: Authenticated user (user_id, email) from JWT token
        auth: Authentication provider

    Returns:
        Tenant information

    Raises:
        401: Not authenticated
        403: User does not have access to this tenant
        404: Tenant not found
    """
    user_id, _email = user_info

    with auth.session_factory() as session:
        # Check if user is global admin
        user = get_user_by_id(session, user_id)
        is_global_admin = user and user.is_global_admin == 1

        # Check tenant access (global admins can access any tenant)
        if not is_global_admin:
            nx = get_nexus_instance()
            if nx and hasattr(nx, "_rebac_manager"):
                if not user_belongs_to_tenant(nx._rebac_manager, user_id, tenant_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Access denied: you are not a member of tenant '{tenant_id}'",
                    )
            else:
                # If ReBAC not available, deny access for non-admins
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: tenant membership verification unavailable",
                )

        tenant = session.get(TenantModel, tenant_id)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{tenant_id}' not found",
            )

        return TenantResponse(
            tenant_id=tenant.tenant_id,
            name=tenant.name,
            domain=tenant.domain,
            description=tenant.description,
            is_active=bool(tenant.is_active),
            created_at=tenant.created_at.isoformat(),
            updated_at=tenant.updated_at.isoformat(),
        )


@router.get("", response_model=TenantListResponse)
async def list_tenants(
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
    limit: int = 100,
    offset: int = 0,
) -> TenantListResponse:
    """List tenants the authenticated user belongs to.

    Global admins can see all tenants. Regular users only see tenants
    they are members of.

    Args:
        user_info: Authenticated user (user_id, email) from JWT token
        auth: Authentication provider
        limit: Maximum number of tenants to return
        offset: Number of tenants to skip

    Returns:
        List of tenants

    Raises:
        401: Not authenticated
    """
    user_id, _email = user_info

    with auth.session_factory() as session:
        # Check if user is global admin
        user = get_user_by_id(session, user_id)
        is_global_admin = user and user.is_global_admin == 1

        if is_global_admin:
            # Global admins see all active tenants
            stmt = (
                select(TenantModel)
                .where(TenantModel.is_active == 1)
                .order_by(TenantModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            tenants = session.scalars(stmt).all()

            # Count total active tenants
            total_stmt = select(TenantModel).where(TenantModel.is_active == 1)
            total = len(session.scalars(total_stmt).all())
        else:
            # Regular users only see tenants they belong to
            nx = get_nexus_instance()
            if nx and hasattr(nx, "_rebac_manager"):
                user_tenant_ids = get_user_tenants(nx._rebac_manager, user_id)
            else:
                user_tenant_ids = []

            if not user_tenant_ids:
                return TenantListResponse(tenants=[], total=0)

            # Query only tenants user belongs to
            stmt = (
                select(TenantModel)
                .where(
                    TenantModel.is_active == 1,
                    TenantModel.tenant_id.in_(user_tenant_ids),
                )
                .order_by(TenantModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            tenants = session.scalars(stmt).all()
            total = len(user_tenant_ids)

        return TenantListResponse(
            tenants=[
                TenantResponse(
                    tenant_id=t.tenant_id,
                    name=t.name,
                    domain=t.domain,
                    description=t.description,
                    is_active=bool(t.is_active),
                    created_at=t.created_at.isoformat(),
                    updated_at=t.updated_at.isoformat(),
                )
                for t in tenants
            ],
            total=total,
        )
