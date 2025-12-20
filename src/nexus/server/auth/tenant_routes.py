"""Tenant management API routes.

Provides endpoints for creating, updating, and managing tenants.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus.server.auth.auth_routes import get_auth_provider
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.tenant_helpers import (
    create_tenant,
    normalize_to_slug,
    suggest_tenant_id,
    validate_tenant_id,
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
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> TenantResponse:
    """Create a new tenant.

    The authenticated user will be added as an admin of the new tenant.

    Args:
        request: Tenant creation request
        auth: Authentication provider

    Returns:
        Created tenant information

    Raises:
        400: Invalid tenant_id or tenant_id already taken
        401: Not authenticated
    """
    # TODO: Add authentication check when auth middleware is ready
    # For now, we'll allow anyone to create tenants for development

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

            # TODO: Add authenticated user to tenant as admin
            # This requires auth middleware to be set up

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
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> TenantResponse:
    """Get tenant information by ID.

    Args:
        tenant_id: Tenant identifier
        auth: Authentication provider

    Returns:
        Tenant information

    Raises:
        404: Tenant not found
    """
    with auth.session_factory() as session:
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
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
    limit: int = 100,
    offset: int = 0,
) -> TenantListResponse:
    """List all active tenants.

    Args:
        auth: Authentication provider
        limit: Maximum number of tenants to return
        offset: Number of tenants to skip

    Returns:
        List of tenants
    """
    with auth.session_factory() as session:
        # Query active tenants
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
