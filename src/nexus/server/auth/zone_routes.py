"""Zone management API routes.

Provides endpoints for creating, updating, and managing zones.

Auth: Uses the unified ``require_auth`` dependency (supports JWT + API key +
static admin key) instead of the legacy JWT-only ``get_authenticated_user``.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
from nexus.bricks.auth.zone_helpers import (
    create_zone,
    normalize_to_slug,
    suggest_zone_id,
    validate_zone_id,
)
from nexus.contracts.zone_phase import ZonePhase
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


def _trigger_federation_remove_zone(nx: Any, zone_id: str) -> bool:
    """Best-effort raft-side zone removal — mirrors the federation_remove_zone
    RPC by reaching through the kernel ``_call`` channel that
    ``FederationRPCService.federation_remove_zone`` uses
    (federation_rpc.py:338).

    Returns True on success, False if the kernel handle is missing or the
    call raised; the failure is logged but never re-raised because the
    caller has already run the SQL deletes by the time this fires and we
    don't want a transient raft hiccup to surface as a 5xx.
    """
    if nx is None:
        return False
    kernel = getattr(nx, "_kernel", None)
    if kernel is None:
        logger.warning(
            "zone %s: federation_remove_zone skipped — kernel handle unavailable",
            zone_id,
        )
        return False
    try:
        kernel._call("federation_remove_zone", {"zone_id": zone_id, "force": False})
    except Exception as exc:
        logger.warning("zone %s: federation_remove_zone failed: %s", zone_id, exc)
        return False
    return True


def _inline_zone_finalizer_deletes(session: Any, zone_id: str) -> None:
    """Run the three zone-scoped DELETEs that previously lived in the K8s-
    finalizer-pattern services (SearchZoneFinalizer for entities +
    relationships, ReBACZoneFinalizer for rebac_tuples).

    Inlined here as part of the K8s-finalizer abstraction simplification
    (PR 7b).  No FK constraints exist between these tables and ``zones``;
    orphan rows only waste storage.  Idempotent — running this twice for
    the same zone is harmless.
    """
    session.execute(
        text("DELETE FROM entities WHERE zone_id = :zid"),
        {"zid": zone_id},
    )
    session.execute(
        text("DELETE FROM relationships WHERE zone_id = :zid"),
        {"zid": zone_id},
    )
    session.execute(
        text("DELETE FROM rebac_tuples WHERE zone_id = :zid"),
        {"zid": zone_id},
    )


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
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> ZoneResponse:
    """Get zone information by ID.

    Only returns zone info if the authenticated user belongs to that zone
    or is a global admin.

    Args:
        zone_id: Zone identifier
        request: FastAPI request (for ``app.state.policy_gate`` lookup)
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        auth: Authentication provider for DB session access

    Returns:
        Zone information

    Raises:
        401: Not authenticated
        403: User does not have access to this zone (after operator deny
            via PolicyGate, or when no gate is configured)
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
            # Issue #3790, Task 19: route zone-scope misses through the
            # approval queue. If an operator approves the request, fall
            # through to the normal lookup path. If the gate is missing,
            # raises an exception, or the operator denies/times-out, fall
            # back to the existing 403 response.
            if not user_belongs_to_zone(
                rebac_mgr, user_id, zone_id
            ) and not await _zone_access_approved_via_gate(
                request=request,
                zone_id=zone_id,
                user_id=user_id,
                auth_result=auth_result,
            ):
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


async def _zone_access_approved_via_gate(
    request: Request,
    zone_id: str,
    user_id: str,
    auth_result: dict[str, Any],
) -> bool:
    """Consult the PolicyGate when a token misses zone scope.

    Issue #3790, Task 19: route zone-scope misses through the approval
    queue. Returns True iff an operator approved the zone access within
    the gate's timeout, in which case the caller may proceed as if the
    membership check had passed. Returns False on missing gate, denial,
    timeout, or any unexpected gate error (graceful degradation — the
    caller then re-raises the original 403).
    """
    gate = getattr(request.app.state, "policy_gate", None)
    if gate is None:
        return False

    # Lazy import keeps this module free of an eager top-level cross-package
    # import (the call site is in nexus.server, not under nexus.bricks/, so
    # the brick boundary checker does not apply — but lazy is still cheaper
    # for the common case where the gate is unset).
    try:
        from nexus.bricks.approvals.models import ApprovalKind, Decision
    except ImportError:
        logger.warning(
            "approvals brick unavailable while resolving zone access for %r; falling back to deny",
            zone_id,
        )
        return False

    # Synthesize stable identifiers from the request's auth_result. The
    # hub's auth_result dict does not currently expose a per-token id, so
    # use subject_id (user_id) as the token identifier and the request's
    # auth source as the session identifier — operators can correlate
    # repeated attempts for the same user/zone in the queue UI.
    #
    # F2 (#3790): the synthesized session_id is deliberately stable across
    # requests (no HTTP-session lifecycle to bind it to). The approvals
    # service guards against this turning a SESSION-scope grant into a
    # durable persist by refusing the SESSION-scope cache fast-path for
    # any session_id starting with ``hub:`` (see
    # ``_is_fabricated_session_id`` in nexus.bricks.approvals.service).
    # Operators that want durable zone access must write a ReBAC tuple
    # via the admin tuples endpoint; an approval here is good for one
    # zone-access attempt only.
    subject_type = auth_result.get("subject_type") or "user"
    token_id = f"hub:{subject_type}:{user_id}"
    session_id = f"{token_id}:zone:{zone_id}"
    try:
        decision = await gate.check(
            kind=ApprovalKind.ZONE_ACCESS,
            subject=zone_id,
            zone_id=zone_id,
            token_id=token_id,
            session_id=session_id,
            agent_id=None,
            reason="zone_access",
            metadata={
                "requested_zone": zone_id,
                "user_id": user_id,
                "subject_type": subject_type,
            },
        )
    except Exception:
        logger.warning(
            "approvals gate raised for zone-access user=%r zone=%r; falling back to deny",
            user_id,
            zone_id,
            exc_info=True,
        )
        return False
    return decision is Decision.APPROVED


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
                    ZoneModel.phase != ZonePhase.TERMINATED,
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
                        ZoneModel.phase != ZonePhase.TERMINATED,
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

    Synchronously tears down the zone in three steps:

    1. ``DELETE FROM entities`` / ``relationships`` / ``rebac_tuples`` for
       this ``zone_id`` (previously the SearchZoneFinalizer +
       ReBACZoneFinalizer SQL).
    2. ``federation_remove_zone`` via the kernel call channel (best-effort
       raft-side teardown — logged-not-raised on failure since the SQL
       has already committed).
    3. Mark ``ZoneModel`` row as ``phase="Terminated"`` + set
       ``deleted_at`` (soft-delete so the row is gone from operator views
       but FK references from audit / api-key history rows remain valid).

    - Active → 202 Accepted (teardown completed)
    - Terminated → 404 Not Found (idempotent retry surfaces this)

    Args:
        zone_id: Zone identifier
        auth_result: Authenticated identity (JWT, API key, or static admin key)
        auth: Authentication provider for DB session access

    Raises:
        403: User is not zone owner or global admin, or zone is ROOT_ZONE_ID
        404: Zone not found or already terminated
    """
    from datetime import UTC, datetime

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

    nx = get_nexus_instance()

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

        if zone.phase == ZonePhase.TERMINATED:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Zone '{zone_id}' is already terminated",
            )

        _inline_zone_finalizer_deletes(session, zone_id)
        _trigger_federation_remove_zone(nx, zone_id)

        zone.phase = ZonePhase.TERMINATED
        zone.finalizers = "[]"
        zone.deleted_at = datetime.now(UTC)
        session.commit()

        return ZoneDeprovisionResponse(
            zone_id=zone_id,
            phase=ZonePhase.TERMINATED,
            finalizers_completed=[],
            finalizers_pending=[],
            finalizers_failed={},
        )
