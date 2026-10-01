"""Zone lifecycle /v2 endpoints (2C, §6.1-6.2 + 6.7).

Contract path is /v2 (§6: deployments may front it as /api/v2 through a
unified base URL — one DTO set, never two). The authenticated
OperationContext is authoritative; request-suggested actor/zone fields are
cross-checked, never trusted (§6.7). Store errors fail closed.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from nexus.contracts.zone_v1 import ZoneCreateRequest, ZonePatchRequest
from nexus.server.api.v2.models.zones import (
    OperationView,
    ZoneCreateBody,
    ZoneListResponse,
    ZonePatchBody,
    ZoneView,
)
from nexus.server.api.v2.zone_security import (
    principal_dict,
    require_global_capability,
    require_zone_capability,
    zone_capability_decision,
)
from nexus.server.dependencies import require_auth
from nexus.services.zones.service import ServiceError, ZoneApplicationService
from nexus.storage.models.auth import ZoneModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2/zones", tags=["zones-v2"])


def _service(request: Request) -> ZoneApplicationService:
    svc = getattr(request.app.state, "zone_application_service", None)
    readiness = getattr(request.app.state, "zone_control_readiness", {})
    if svc is None or not readiness.get("composite_armed", False):
        raise HTTPException(status_code=503, detail="zone service not armed")
    return cast(ZoneApplicationService, svc)


def _svc_error(exc: ServiceError) -> HTTPException:
    detail: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
    }
    if exc.details is not None:
        detail["details"] = exc.details
    return HTTPException(status_code=exc.http_status, detail=detail)


def _iso(value: Any) -> str:
    return value.isoformat() if value is not None else ""


def _view(zone: ZoneModel) -> ZoneView:
    return ZoneView(
        zone_id=zone.zone_id,
        display_name=zone.display_name or zone.name,
        description=zone.description,
        status=zone.canonical_status or "unknown",
        deployment=(
            {
                k: v
                for k, v in {
                    "location": zone.placement_location,
                    "data_domain": zone.placement_data_domain,
                    "trust_domain": zone.trust_domain,
                    "region": zone.placement_region,
                }.items()
                if v is not None
            }
            or {"location": "cloud", "trust_domain": "local"}
        ),
        labels=zone.labels,
        revision=zone.canonical_revision or "",
        created_by={
            key: value
            for key, value in (
                zone.created_by or {"subject_type": "service", "subject_id": "legacy"}
            ).items()
            if key in {"subject_type", "subject_id", "trust_domain"}
        },
        created_at=_iso(zone.created_at),
        updated_at=_iso(zone.updated_at),
        deleted_at=_iso(zone.deleted_at) or None,
    )


@router.post("", status_code=202)
def create_zone(
    body: ZoneCreateBody,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    """202 + Location → operation; a synchronous completion still keeps the
    operation (§6.2). The zone never reads active until a runtime receipt."""
    svc = _service(request)
    require_global_capability(auth_result, "zone.global.create")
    principal = principal_dict(auth_result)
    # Body fields are contract-typed (H-2): admission-shape violations were
    # rejected at the validation layer (422), so this construction cannot
    # raise; the frozen schema also makes deployment.location/trust_domain
    # required — no server-side defaulting (H-2 consistency).
    contract = ZoneCreateRequest(
        api_version="auth.sudo.dev/v1",
        kind="ZoneCreateRequest",
        zone_id=body.zone_id,
        display_name=body.display_name,
        description=body.description,
        deployment=body.deployment,
        labels=body.labels,
    )
    try:
        result = svc.create_zone(contract, idempotency_key=idempotency_key, principal=principal)
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return OperationView(
        operation_id=result.operation_id,
        action="create",
        state=result.state,
        step=result.step,
        retryable=result.retryable,
    )


@router.get("")
def list_zones(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    status: str | None = None,
    location: str | None = None,
    data_domain: str | None = None,
    trust_domain: str | None = None,
    include_deleted: bool = False,
    all_zones: bool = False,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneListResponse:
    """Caller-visible zones only; no count leakage of invisible zones."""
    from sqlalchemy import select

    _service(request)  # arming check
    session = getattr(request.app.state, "zone_session_factory", None)
    if session is None:
        raise HTTPException(status_code=503, detail="zone store unavailable")
    # L-8①: ?status=deleted reads the tombstones without include_deleted —
    # that is an audit-gated read and must hit the same capability + event.
    if include_deleted or all_zones or status == "deleted":
        require_global_capability(auth_result, "zone.audit.read")
        from nexus.contracts.protocols.activity import EventKind, Result, emit

        emit(
            kind=EventKind.ZONE_ACCESS,
            result=Result.OK,
            actor_user=str(auth_result.get("subject_id") or "unknown"),
            meta={"operation": "zone.list.all", "include_deleted": include_deleted},
        )
    needs_visibility = not all_zones and not auth_result.get("is_admin", False)
    authorized_zone_ids: set[str] | None = None
    if needs_visibility:
        # M-18: one grant prefilter — non-admin visibility REQUIRES an active
        # grant, so grantless rows are dropped before the per-row two-layer
        # decision runs.  Replaces the full-table authorization fan-out that
        # made cheap polling expensive for everyone.
        from nexus.storage.models import ZoneGrantModel

        with session() as s:
            authorized_zone_ids = set(
                s.execute(select(ZoneGrantModel.zone_id).where(ZoneGrantModel.status == "active"))
                .scalars()
                .all()
            )
    visible: list[ZoneModel] = []
    page_cursor = cursor
    with session() as s:
        # M-18: SQL-side cursor pagination (limit+1 pages) instead of one
        # unbounded full-table fetch; keeps collecting visible rows across
        # pages until the limit is met or the table is exhausted.
        while len(visible) <= limit:
            stmt = select(ZoneModel).order_by(ZoneModel.zone_id)
            if status is not None:
                stmt = stmt.where(ZoneModel.canonical_status == status)
            elif not include_deleted:
                stmt = stmt.where(ZoneModel.canonical_status != "deleted")
            if location is not None:
                stmt = stmt.where(ZoneModel.placement_location == location)
            if data_domain is not None:
                stmt = stmt.where(ZoneModel.placement_data_domain == data_domain)
            if trust_domain is not None:
                stmt = stmt.where(ZoneModel.trust_domain == trust_domain)
            if page_cursor:
                stmt = stmt.where(ZoneModel.zone_id > page_cursor)
            page = s.execute(stmt.limit(limit + 1)).scalars().all()
            if not page:
                break
            for zone in page:
                if authorized_zone_ids is not None and zone.zone_id not in authorized_zone_ids:
                    continue
                if not zone_capability_decision(
                    request,
                    auth_result,
                    zone_id=zone.zone_id,
                    capability="zone.data.read",
                ):
                    continue
                visible.append(zone)
                if len(visible) > limit:
                    break
            if len(page) <= limit:
                break
            page_cursor = page[-1].zone_id
    has_more = len(visible) > limit
    rows = visible[:limit]
    return ZoneListResponse(
        zones=[_view(z) for z in rows],
        next_cursor=rows[-1].zone_id if has_more and rows else None,
    )


@router.get("/{zone_id}")
def get_zone(
    zone_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneView:
    _service(request)  # arming check — read paths still require the service
    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.data.read")
    with _zone_session(request) as s:
        zone = s.get(ZoneModel, zone_id)
        if zone is None:
            raise HTTPException(
                status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False}
            )
        return _view(zone)


@router.get("/{zone_id}/status")
def zone_status(
    zone_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.data.read")
    with _zone_session(request) as s:
        zone = s.get(ZoneModel, zone_id)
        if zone is None:
            raise HTTPException(
                status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False}
            )
        return {
            "zone_id": zone.zone_id,
            "status": zone.canonical_status or "unknown",
            "runtime_health": zone.runtime_health or "unknown",
            "runtime_observed_at": _iso(zone.runtime_observed_at) or None,
            "revision": zone.canonical_revision or "",
        }


@router.patch("/{zone_id}")
def patch_zone(
    zone_id: str,
    body: ZonePatchBody,
    request: Request,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneView:
    svc = _service(request)
    require_zone_capability(
        request, auth_result, zone_id=zone_id, capability="zone.metadata.manage"
    )
    contract = ZonePatchRequest(
        api_version="auth.sudo.dev/v1",
        kind="ZonePatchRequest",
        **body.model_dump(exclude_unset=True),
    )
    try:
        new_revision = svc.patch_zone(
            zone_id,
            contract,
            revision_if_match=if_match,
            idempotency_key=idempotency_key,
            principal=principal_dict(auth_result),
        )
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    with _zone_session(request) as s:
        zone = s.get(ZoneModel, zone_id)
        assert zone is not None
        view = _view(zone)
    view.revision = new_revision
    response.headers["ETag"] = new_revision
    return view


@router.post("/{zone_id}:suspend", status_code=202)
def suspend_zone(
    zone_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),  # noqa: ARG001
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    return _lifecycle(zone_id, "suspend", request, auth_result, idempotency_key)


@router.post("/{zone_id}:resume", status_code=202)
def resume_zone(
    zone_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),  # noqa: ARG001
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    return _lifecycle(zone_id, "resume", request, auth_result, idempotency_key)


def _lifecycle(
    zone_id: str,
    action: str,
    request: Request,
    auth_result: dict[str, Any],
    idempotency_key: str,
) -> OperationView:
    svc = _service(request)
    require_zone_capability(
        request, auth_result, zone_id=zone_id, capability="zone.metadata.manage"
    )
    principal = principal_dict(auth_result)
    try:
        result = getattr(svc, f"{action}_zone")(
            zone_id, principal=principal, idempotency_key=idempotency_key
        )
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    return OperationView(
        operation_id=result.operation_id,
        action=action,
        state=result.state,
        step=result.step,
        retryable=result.retryable,
    )


@router.delete("/{zone_id}", status_code=202)
def request_deprovision(
    zone_id: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),  # noqa: ARG001
    confirmation: str = Header(alias="X-Nexus-Confirm-Zone"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    """DELETE requests deprovision; it never wipes rows inline (§6.2)."""
    svc = _service(request)
    require_global_capability(auth_result, "zone.lifecycle.delete")
    if confirmation != zone_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ZONE_DELETE_BLOCKED",
                "message": "X-Nexus-Confirm-Zone must exactly match zone_id",
                "retryable": False,
            },
        )
    principal = principal_dict(auth_result)
    try:
        result = svc.request_deprovision(
            zone_id, principal=principal, idempotency_key=idempotency_key
        )
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return OperationView(
        operation_id=result.operation_id,
        action="deprovision",
        state=result.state,
        step=result.step,
        retryable=result.retryable,
    )


def _zone_session(request: Request) -> Session:
    factory = getattr(request.app.state, "zone_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="zone store unavailable")
    return cast(Session, factory())
