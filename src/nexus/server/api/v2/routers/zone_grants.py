"""Grant, operation, capability and delegation /v2 endpoints (2C, §6.3-6.5)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from nexus.contracts.zone_v1 import PrincipalRef, ZoneGrantCreateRequest, ZoneGrantSource
from nexus.server.api.v2.models.zones import (
    DelegationIssueBody,
    DelegationView,
    GrantCreateBody,
    GrantListResponse,
    GrantView,
    OperationView,
)
from nexus.server.api.v2.zone_security import (
    has_global_capability,
    principal_dict,
    principal_from_auth,
    require_zone_capability,
)
from nexus.server.dependencies import require_auth
from nexus.services.zones.authz import AuthorizationService, Principal
from nexus.services.zones.service import ServiceError, ZoneApplicationService
from nexus.storage.models import ZoneDelegationModel, ZoneGrantModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2", tags=["zone-grants-v2"])


def _svc_error(exc: ServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def _service(request: Request) -> ZoneApplicationService:
    svc = getattr(request.app.state, "zone_application_service", None)
    readiness = getattr(request.app.state, "zone_control_readiness", {})
    if svc is None or not readiness.get("composite_armed", False):
        raise HTTPException(status_code=503, detail="zone service not armed")
    return cast(ZoneApplicationService, svc)


def _authz(request: Request) -> AuthorizationService:
    svc = getattr(request.app.state, "zone_authorization_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="authorization service not armed")
    return cast(AuthorizationService, svc)


def _session(request: Request) -> Session:
    factory = getattr(request.app.state, "zone_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="zone store unavailable")
    return cast(Session, factory())


def _grant_view(g: ZoneGrantModel) -> GrantView:
    return GrantView(
        grant_id=g.grant_id,
        zone_id=g.zone_id,
        grantee=g.grantee,
        capabilities=g.capabilities,
        resource_prefixes=g.resource_prefixes,
        source={"source_type": g.source_type, "source_id": g.source_id},
        issued_by=g.issued_by,
        reason=g.reason,
        policy_version=g.policy_version,
        revision=g.revision,
        status=g.status,
        created_at=g.created_at.isoformat() if g.created_at else "",
        not_before=g.not_before.isoformat() if g.not_before else None,
        expires_at=g.expires_at.isoformat() if g.expires_at else None,
        revoked_at=g.revoked_at.isoformat() if g.revoked_at else None,
        revoked_by=g.revoked_by,
        revoke_reason=g.revoke_reason,
    )


def _optional_iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


@router.post("/zones/{zone_id}/grants", status_code=202)
def create_grant(
    zone_id: str,
    body: GrantCreateBody,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    svc = _service(request)
    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.grants.manage")
    principal = principal_dict(auth_result)
    contract = ZoneGrantCreateRequest(
        api_version="auth.sudo.dev/v1",
        kind="ZoneGrantCreateRequest",
        grantee=PrincipalRef.model_validate(body.grantee),
        capabilities=body.capabilities,
        resource_prefixes=body.resource_prefixes,
        source=ZoneGrantSource.model_validate(body.source) if body.source else None,
        reason=body.reason,
        policy_version=body.policy_version,
        not_before=body.not_before,
        expires_at=body.expires_at,
    )
    try:
        result = svc.issue_grant(
            zone_id, contract, idempotency_key=idempotency_key, principal=principal
        )
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return OperationView(
        operation_id=result.operation_id,
        action="grant",
        state=result.state,
        step=result.step,
        retryable=result.retryable,
    )


@router.get("/zones/{zone_id}/grants")
def list_grants(
    zone_id: str,
    request: Request,
    grantee: str | None = None,
    status: str | None = None,
    source: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> GrantListResponse:
    """Requires zone.grants.manage or an audit capability (§6.3)."""
    from sqlalchemy import select

    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.grants.manage")

    with _session(request) as s:
        stmt = select(ZoneGrantModel).where(ZoneGrantModel.zone_id == zone_id)
        if status:
            stmt = stmt.where(ZoneGrantModel.status == status)
        if source:
            stmt = stmt.where(ZoneGrantModel.source_type == source)
        if cursor:
            stmt = stmt.where(ZoneGrantModel.grant_id > cursor)
        rows = s.execute(stmt.order_by(ZoneGrantModel.grant_id)).scalars().all()
        if grantee:
            rows = [row for row in rows if row.grantee.get("subject_id") == grantee]
    has_more = len(rows) > limit
    rows = rows[:limit]
    return GrantListResponse(
        grants=[_grant_view(g) for g in rows],
        next_cursor=rows[-1].grant_id if has_more and rows else None,
    )


@router.get("/zones/{zone_id}/grants/{grant_id}")
def get_grant(
    zone_id: str,
    grant_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> GrantView:
    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.grants.manage")

    with _session(request) as s:
        g = s.get(ZoneGrantModel, grant_id)
        if g is None or g.zone_id != zone_id:
            raise HTTPException(
                status_code=404, detail={"code": "GRANT_NOT_FOUND", "retryable": False}
            )
        return _grant_view(g)


@router.delete("/zones/{zone_id}/grants/{grant_id}", status_code=202)
def revoke_grant(
    zone_id: str,
    grant_id: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),  # noqa: ARG001
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Revoke; the response carries the operation and authorization revision."""
    svc = _service(request)
    require_zone_capability(request, auth_result, zone_id=zone_id, capability="zone.grants.manage")
    principal = principal_dict(auth_result)
    try:
        result = svc.revoke_grant(
            zone_id,
            grant_id,
            principal=principal,
            reason="api revoke",
            idempotency_key=idempotency_key,
        )
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    with _session(request) as s:
        g = s.get(ZoneGrantModel, grant_id)
        auth_revision = g.revision if g else ""
    response.headers["Location"] = f"/v2/zone-operations/{result.operation_id}"
    return {
        "operation_id": result.operation_id,
        "authorization_revision": auth_revision,
        "grant_id": grant_id,
        "status": "revoked",
    }


@router.get("/zone-operations/{operation_id}")
def get_operation(
    operation_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> OperationView:
    svc = _service(request)
    op = svc.get_operation(operation_id)
    if op is None:
        raise HTTPException(status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False})
    if not auth_result.get("is_admin", False) and op.get("principal_id") != auth_result.get(
        "subject_id"
    ):
        raise HTTPException(status_code=404, detail={"code": "ZONE_NOT_FOUND", "retryable": False})
    return OperationView(
        operation_id=op["operation_id"],
        action=op["action"],
        zone_id=op.get("zone_id"),
        grant_id=op.get("grant_id"),
        state=op["state"],
        step=op["step"],
        retryable=op["retryable"],
        error=op.get("error"),
        created_at=_optional_iso(op.get("created_at")),
        updated_at=_optional_iso(op.get("updated_at")),
        completed_at=_optional_iso(op.get("completed_at")),
    )


@router.get("/zone-capabilities")
def zone_capabilities(
    request: Request,  # noqa: ARG001
    auth_result: dict[str, Any] = Depends(require_auth),  # noqa: ARG001
) -> dict[str, Any]:
    from nexus.contracts.zone_v1 import KNOWN_CAPABILITIES, KNOWN_ERROR_CODES

    return {
        "api_version": "auth.sudo.dev/v1",
        "known_capabilities": sorted(KNOWN_CAPABILITIES),
        "known_error_codes": sorted(KNOWN_ERROR_CODES),
        "providers": dict(getattr(request.app.state, "zone_control_readiness", {})),
    }


@router.post("/auth/zone-delegations", status_code=201)
def issue_delegation(
    body: DelegationIssueBody,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> DelegationView:
    """Trusted Moss issuance service identity only (§6.4)."""
    authz = _authz(request)
    issuer = principal_from_auth(auth_result)
    try:
        with _session(request) as s, s.begin():
            d = authz.issue_delegation(
                s,
                principal=Principal(subject_type="user", subject_id=body.user_id),
                issuer=issuer,
                org_id=body.org_id,
                membership_version=body.membership_version,
                zone_id=body.zone_id,
                audience=body.audience,
                ttl_s=body.ttl_s,
                idempotency_key=idempotency_key,
            )
    except ServiceError as exc:
        raise _svc_error(exc) from exc
    return DelegationView(
        delegation_id=d.delegation_id,
        user_id=d.user_id,
        org_id=d.org_id,
        zone_id=d.zone_id,
        grant_id=d.grant_id,
        grant_revision=d.grant_revision,
        authorization_epoch=int(d.epoch),
        audience=d.audience,
        expires_at=d.expires_at.isoformat(),
        status=d.status,
    )


@router.get("/auth/zone-delegations/{delegation_id}")
def get_delegation(
    delegation_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    authz = _authz(request)
    with _session(request) as s:
        d = s.get(ZoneDelegationModel, delegation_id)
        if d is None:
            raise HTTPException(
                status_code=404, detail={"code": "GRANT_NOT_FOUND", "retryable": False}
            )
        if not auth_result.get("is_admin", False) and d.user_id != auth_result.get("subject_id"):
            raise HTTPException(
                status_code=404, detail={"code": "GRANT_NOT_FOUND", "retryable": False}
            )
        decision = authz.verify_delegation(s, delegation_id=delegation_id, audience=d.audience)
        if not decision:
            raise HTTPException(
                status_code=403,
                detail={"code": decision.code, "message": decision.reason, "retryable": False},
            )
        return {
            "delegation_id": d.delegation_id,
            "user_id": d.user_id,
            "org_id": d.org_id,
            "zone_id": d.zone_id,
            "grant_id": d.grant_id,
            "grant_revision": d.grant_revision,
            "authorization_epoch": int(d.epoch),
            "audience": d.audience,
            "status": d.status,
            "expires_at": d.expires_at.isoformat(),
        }


@router.delete("/auth/zone-delegations/{delegation_id}", status_code=202)
def revoke_delegation(
    delegation_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),  # noqa: ARG001
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    authz = _authz(request)
    with _session(request) as s, s.begin():
        existing = s.get(ZoneDelegationModel, delegation_id)
        if existing is None or (
            not auth_result.get("is_admin", False)
            and existing.user_id != auth_result.get("subject_id")
            and not has_global_capability(auth_result, "zone.delegations.revoke")
        ):
            raise HTTPException(
                status_code=404, detail={"code": "GRANT_NOT_FOUND", "retryable": False}
            )
        ok = authz.revoke_delegation(s, delegation_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": "GRANT_NOT_FOUND", "retryable": False})
    return {"delegation_id": delegation_id, "status": "revoked"}
