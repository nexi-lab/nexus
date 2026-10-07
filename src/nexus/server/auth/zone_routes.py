"""Zone management API routes.

Provides endpoints for creating, updating, and managing zones.

Auth: Uses the unified ``require_auth`` dependency (supports JWT + API key +
static admin key) instead of the legacy JWT-only ``get_authenticated_user``.

Temporary zone access for a user without a standing grant is expressed as a
short-TTL grant (``POST /v2/zones/{zone_id}/grants`` with ``expires_at``) —
auditable, revocable, and identical on both doors. The one-shot PolicyGate
bypass (Issue #3790 Task 19) was removed with the v2 authorization model.
"""

import logging
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from nexus.bricks.auth.zone_helpers import normalize_to_slug, suggest_zone_id
from nexus.contracts.zone_phase import ZonePhase
from nexus.server.api.v2.zone_security import (
    require_global_capability,
    require_zone_capability,
    zone_capability_decision,
)
from nexus.server.auth.auth_routes import get_auth_provider
from nexus.server.dependencies import require_auth
from nexus.services.zones.service import ServiceError, ZoneApplicationService
from nexus.storage.models import ZoneModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/zones", tags=["zones"])


def _deprecation_headers(response: Response) -> None:
    """§6.8: legacy /api/zones keeps one stable release window, then goes."""
    response.headers.setdefault("Deprecation", "true")
    response.headers.setdefault("Sunset", "Sat, 18 Sep 2027 00:00:00 GMT")
    response.headers.setdefault("Link", '</v2/zones>; rel="successor-version"')


def _zone_service(request: Request) -> ZoneApplicationService:
    """Return the one ZoneApplicationService used by both legacy and v2."""
    service = getattr(request.app.state, "zone_application_service", None)
    readiness = getattr(request.app.state, "zone_control_readiness", {})
    if service is None or not readiness.get("composite_armed", False):
        raise HTTPException(status_code=503, detail="zone service not armed")
    return cast(ZoneApplicationService, service)


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
    zone_request: CreateZoneRequest,
    request: Request,
    response: Response,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneResponse:
    """Create a new zone.

    Delegates to the single ZoneApplicationService (§6.8: the legacy route is
    an adapter, never a second writer). The zone becomes visible here only
    after the runtime receipt marks it active.

    Args:
        zone_request: Zone creation request body
        request: FastAPI request used to resolve the app-scoped DB session
        response: Response object carrying deprecation metadata
        auth_result: Authenticated identity (JWT, API key, or static admin key)

    Returns:
        Created zone information

    Raises:
        400: Invalid zone_id or zone_id already taken
        401: Not authenticated
        500: Failed to assign creator as zone owner
    """
    _deprecation_headers(response)
    user_id = auth_result["subject_id"]

    svc = _zone_service(request)
    if svc is not None:
        # Admission gate aligned with v2 (zones.py require_global_capability):
        # the legacy adapter must not be a softer door to the same writer.
        require_global_capability(auth_result, "zone.global.create")
        if zone_request.domain:
            # The v2 contract carries no domain field — refuse loudly instead
            # of silently accepting and dropping the value.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_REQUEST",
                    "message": "domain is not supported: v2 zones carry no domain field",
                    "retryable": False,
                },
            )
        from nexus.contracts.zone_v1 import ZoneCreateRequest as ContractCreate

        if not zone_request.zone_id:
            suggested = normalize_to_slug(zone_request.name)
            session_factory = _get_session_factory(request)
            with session_factory() as session:
                zone_id = suggest_zone_id(suggested, session)
        else:
            zone_id = zone_request.zone_id
        try:
            contract = ContractCreate(
                api_version="auth.sudo.dev/v1",
                kind="ZoneCreateRequest",
                zone_id=zone_id,
                display_name=zone_request.name,
                description=zone_request.description,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_ZONE_ID",
                    "message": f"zone_id {zone_id!r} failed contract validation",
                    "retryable": False,
                },
            ) from exc
        principal = {
            "subject_type": str(auth_result.get("subject_type") or "user"),
            "subject_id": user_id,
            "is_admin": bool(auth_result.get("is_admin", False)),
        }
        try:
            operation = svc.create_zone(
                contract, idempotency_key=f"legacy:{zone_id}", principal=principal
            )
        except ServiceError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
            ) from exc
        response.headers["Location"] = f"/v2/zone-operations/{operation.operation_id}"
        session_factory = _get_session_factory(request)
        with session_factory() as session:
            zone = session.get(ZoneModel, zone_id)
            if zone is not None:
                # reflect the canonical lifecycle in the legacy shape
                zone.phase = (
                    ZonePhase.ACTIVE
                    if zone.canonical_status == "active"
                    else ZonePhase.TERMINATING
                    if zone.canonical_status == "deleting"
                    else zone.phase
                )
                if zone.canonical_status != "active":
                    response.status_code = status.HTTP_202_ACCEPTED
                return _zone_to_response(zone)
        response.status_code = status.HTTP_202_ACCEPTED
        # Zone row not visible yet (saga still running): the legacy shape gets
        # a non-Active phase rather than a phantom Active (C6: no lying).
        return ZoneResponse(
            zone_id=zone_id,
            name=zone_request.name,
            description=zone_request.description,
            phase="Creating",
            is_active=False,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )


@router.get("/{zone_id}", response_model=ZoneResponse)
async def get_zone(
    zone_id: str,
    request: Request,
    response: Response,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneResponse:
    """Get zone information by ID.

    Only returns zone info if the authenticated user belongs to that zone
    or is a global admin.

    Args:
        zone_id: Zone identifier
        request: FastAPI request for policy-gate and DB-session lookup
        auth_result: Authenticated identity (JWT, API key, or static admin key)

    Returns:
        Zone information

    Raises:
        401: Not authenticated
        403: User does not have access to this zone (after operator deny
            via PolicyGate, or when no gate is configured)
        404: Zone not found
    """
    is_admin = auth_result.get("is_admin", False)

    _deprecation_headers(response)
    _zone_service(request)
    if not is_admin:
        require_zone_capability(
            request,
            auth_result,
            zone_id=zone_id,
            capability="zone.data.read",
        )

    session_factory = _get_session_factory(request)
    with session_factory() as session:
        zone = session.get(ZoneModel, zone_id)
        if not zone:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Zone '{zone_id}' not found",
            )

        return _zone_to_response(zone)


def _get_session_factory(request: Request) -> Any:
    """Resolve the app-scoped, provider-backed, or NexusFS DB session factory."""
    sf = getattr(request.app.state, "session_factory", None)
    if sf is not None:
        return sf

    provider = getattr(request.app.state, "auth_provider", None)
    sf = getattr(provider, "session_factory", None)
    if sf is not None:
        return sf
    for attr in ("providers", "_providers"):
        for child in getattr(provider, attr, ()) or ():
            sf = getattr(child, "session_factory", None)
            if sf is not None:
                return sf

    try:
        auth = get_auth_provider()
        sf = getattr(auth, "session_factory", None)
        if sf is not None:
            return sf
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
    response: Response,
    auth_result: dict[str, Any] = Depends(require_auth),
    limit: int = 100,
    offset: int = 0,
) -> ZoneListResponse:
    """List zones the authenticated user belongs to.

    Global admins can see all zones. Regular users only see zones
    they are members of. Works with JWT and API-key authentication.

    Args:
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        limit: Maximum number of zones to return
        offset: Number of zones to skip

    Returns:
        List of zones

    Raises:
        401: Not authenticated
    """
    # §6.8: every legacy-route response carries deprecation markers.
    _deprecation_headers(response)
    is_admin = auth_result.get("is_admin", False)

    _zone_service(request)
    session_factory = _get_session_factory(request)
    with session_factory() as session:
        if is_admin:
            # Global admins see all active zones
            stmt = (
                select(ZoneModel)
                .where(ZoneModel.phase != ZonePhase.TERMINATED)
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
                    .where(ZoneModel.phase != ZonePhase.TERMINATED)
                )
                or 0
            )
        else:
            # Same grant prefilter as the v2 list (M-18 pattern): a non-admin
            # row without an active grant can never pass the two-layer
            # decision, so drop it in SQL before the per-row fan-out.
            from nexus.storage.models import ZoneGrantModel

            granted = set(
                session.execute(
                    select(ZoneGrantModel.zone_id).where(ZoneGrantModel.status == "active")
                )
                .scalars()
                .all()
            )
            candidates = session.scalars(
                select(ZoneModel)
                .where(ZoneModel.phase != ZonePhase.TERMINATED, ZoneModel.zone_id.in_(granted))
                .order_by(ZoneModel.created_at.desc())
            ).all()
            visible = [
                zone
                for zone in candidates
                if zone_capability_decision(
                    request,
                    auth_result,
                    zone_id=zone.zone_id,
                    capability="zone.data.read",
                ).allowed
            ]
            total = len(visible)
            zones = visible[offset : offset + limit]

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
    request: Request,
    response: Response,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneDeprovisionResponse:
    """Delete (deprovision) a zone.

    Delegates to the single ZoneApplicationService (§6.8): DELETE
    requests deprovisioning — blockers surface as 409, and the zone reaches
    Terminated only through the deprovision operation, never as a best-effort
    side effect of an HTTP call.

    Args:
        zone_id: Zone identifier
        request: FastAPI request used to resolve the app-scoped DB session
        response: Response object carrying deprecation metadata + operation
        auth_result: Authenticated identity (JWT, API key, or static admin key)

    Raises:
        403: User is not zone owner or global admin, or zone is ROOT_ZONE_ID
        404: Zone not found or already terminated
        409: Deprovision blocked (active grants, retention, ...)
    """
    _deprecation_headers(response)
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

    svc = _zone_service(request)
    if svc is not None:
        from nexus.services.zones.service import ServiceError

        require_zone_capability(
            request,
            auth_result,
            zone_id=zone_id,
            capability="zone.lifecycle.delete",
        )

        principal = {
            "subject_type": str(auth_result.get("subject_type") or "user"),
            "subject_id": auth_result["subject_id"],
            "is_admin": bool(auth_result.get("is_admin", False)),
        }
        try:
            operation = svc.request_deprovision(zone_id, principal=principal)
        except ServiceError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            ) from exc
        response.headers["Location"] = f"/v2/zone-operations/{operation.operation_id}"
        return ZoneDeprovisionResponse(
            zone_id=zone_id,
            phase=ZonePhase.TERMINATING,
            finalizers_completed=[],
            finalizers_pending=["zone-deprovision-operation"],
            finalizers_failed={},
        )
