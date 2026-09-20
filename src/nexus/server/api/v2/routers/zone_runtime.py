"""Operator-only join/mount and policy-gated cross-zone transfer endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import or_, select

from nexus.server.api.v2.models.zones import (
    OperationView,
    ZoneJoinBody,
    ZoneMountCreateBody,
    ZoneMountListResponse,
    ZoneMountView,
    ZoneTransferBody,
)
from nexus.server.api.v2.zone_security import (
    principal_dict,
    require_global_capability,
    require_zone_capability,
)
from nexus.server.dependencies import require_auth
from nexus.services.zones.service import ServiceError, ZoneApplicationService
from nexus.storage.models import ZoneMountModel

router = APIRouter(prefix="/v2", tags=["zone-runtime-v2"])


def _service(request: Request) -> ZoneApplicationService:
    svc = getattr(request.app.state, "zone_application_service", None)
    readiness = getattr(request.app.state, "zone_control_readiness", {})
    if svc is None or not readiness.get("composite_armed", False):
        raise HTTPException(status_code=503, detail="zone service not armed")
    return cast(ZoneApplicationService, svc)


def _error(exc: ServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def _operation(result: Any, action: str) -> OperationView:
    return OperationView(
        operation_id=result.operation_id,
        action=action,
        state=result.state,
        step=result.step,
        retryable=result.retryable,
    )


@router.post("/zones/{zone_id}/joins", status_code=202)
def join_zone(
    zone_id: str,
    body: ZoneJoinBody,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    require_global_capability(auth_result, "zone.runtime.join")
    try:
        result = _service(request).request_join(
            zone_id,
            peers=body.peers,
            learner=body.learner,
            idempotency_key=idempotency_key,
            principal=principal_dict(auth_result),
        )
    except ServiceError as exc:
        raise _error(exc) from exc
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return _operation(result, "create")


@router.post("/zone-mounts", status_code=202)
def create_mount(
    body: ZoneMountCreateBody,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    require_zone_capability(
        request,
        auth_result,
        zone_id=body.parent_zone_id,
        capability="zone.metadata.manage",
        resource_path=body.path,
    )
    require_zone_capability(
        request,
        auth_result,
        zone_id=body.target_zone_id,
        capability="zone.data.read",
    )
    try:
        result = _service(request).request_mount(
            parent_zone_id=body.parent_zone_id,
            target_zone_id=body.target_zone_id,
            path=body.path,
            idempotency_key=idempotency_key,
            principal=principal_dict(auth_result),
        )
    except ServiceError as exc:
        raise _error(exc) from exc
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return _operation(result, "mount")


@router.delete("/zone-mounts/{mount_id}", status_code=202)
def delete_mount(
    mount_id: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    factory = getattr(request.app.state, "zone_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="zone store unavailable")
    with factory() as session:
        mount = session.get(ZoneMountModel, mount_id)
        if mount is None:
            raise HTTPException(
                status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False}
            )
        parent_zone_id = mount.parent_zone_id
        path = mount.path
    require_zone_capability(
        request,
        auth_result,
        zone_id=parent_zone_id,
        capability="zone.metadata.manage",
        resource_path=path,
    )
    try:
        result = _service(request).request_unmount(
            mount_id,
            idempotency_key=idempotency_key,
            principal=principal_dict(auth_result),
        )
    except ServiceError as exc:
        raise _error(exc) from exc
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return _operation(result, "unmount")


@router.get("/zone-mounts")
def list_mounts(
    request: Request,
    zone_id: str,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> ZoneMountListResponse:
    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.data.read")
    factory = getattr(request.app.state, "zone_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="zone store unavailable")
    with factory() as session:
        stmt = select(ZoneMountModel).where(
            or_(
                ZoneMountModel.parent_zone_id == zone_id,
                ZoneMountModel.target_zone_id == zone_id,
            )
        )
        if cursor:
            stmt = stmt.where(ZoneMountModel.mount_id > cursor)
        rows = (
            session.execute(stmt.order_by(ZoneMountModel.mount_id).limit(limit + 1)).scalars().all()
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ZoneMountListResponse(
        mounts=[
            ZoneMountView(
                mount_id=row.mount_id,
                parent_zone_id=row.parent_zone_id,
                target_zone_id=row.target_zone_id,
                path=row.path,
                desired_state=row.desired_state,
                observed_state=row.observed_state,
                runtime_revision=row.runtime_revision,
            )
            for row in rows
        ],
        next_cursor=rows[-1].mount_id if has_more and rows else None,
    )


@router.post("/zone-transfers", status_code=202)
def create_transfer(
    body: ZoneTransferBody,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    source = body.source.model_dump(mode="json")
    target = body.target.model_dump(mode="json")
    require_zone_capability(
        request,
        auth_result,
        zone_id=str(source["zone_id"]),
        capability="zone.data.read",
        resource_path=str(source["path"]),
    )
    require_zone_capability(
        request,
        auth_result,
        zone_id=str(target["zone_id"]),
        capability="zone.data.write",
        resource_path=str(target["path"]),
    )
    try:
        result = _service(request).request_transfer(
            source=source,
            target=target,
            idempotency_key=idempotency_key,
            principal=principal_dict(auth_result),
        )
    except ServiceError as exc:
        raise _error(exc) from exc
    response.headers["Location"] = f"/v2/zone-transfers/{result.operation_id}"
    return _operation(result, "transfer")


@router.get("/zone-transfers/{operation_id}")
def get_transfer(
    operation_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    op = _service(request).get_operation(operation_id)
    if op is None or op["action"] != "transfer":
        raise HTTPException(status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False})
    if not auth_result.get("is_admin", False) and op.get("principal_id") != auth_result.get(
        "subject_id"
    ):
        raise HTTPException(status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False})
    return OperationView(**{key: value for key, value in op.items() if key != "principal_id"})
