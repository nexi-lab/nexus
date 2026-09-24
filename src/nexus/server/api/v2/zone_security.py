"""Authentication and authorization helpers for the Zone v1 HTTP surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from nexus.services.zones.authz import Principal


@dataclass(frozen=True)
class VerifiedZoneDelegation:
    """Authoritative delegation snapshot used to bind a runtime descriptor."""

    delegation_id: str
    user_id: str
    org_id: str
    zone_id: str
    grant_id: str
    grant_revision: str
    authorization_epoch: int


def principal_from_auth(auth_result: dict[str, Any]) -> Principal:
    subject_id = auth_result.get("subject_id")
    if not subject_id:
        raise HTTPException(status_code=403, detail="authenticated principal has no subject id")
    return Principal(
        subject_type=str(auth_result.get("subject_type") or "user"),
        subject_id=str(subject_id),
        trust_domain=(
            str(auth_result["trust_domain"]) if auth_result.get("trust_domain") else None
        ),
    )


def principal_dict(auth_result: dict[str, Any]) -> dict[str, Any]:
    principal = principal_from_auth(auth_result)
    return principal.as_json() | {"is_admin": bool(auth_result.get("is_admin", False))}


def has_global_capability(auth_result: dict[str, Any], capability: str) -> bool:
    if auth_result.get("is_admin", False):
        return True
    values = set(auth_result.get("capabilities") or ())
    metadata = auth_result.get("metadata")
    if isinstance(metadata, dict):
        values.update(metadata.get("capabilities") or ())
    return capability in values


def require_global_capability(auth_result: dict[str, Any], capability: str) -> None:
    if not has_global_capability(auth_result, capability):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "RESOURCE_RELATION_DENIED",
                "message": f"global capability {capability} is required",
                "retryable": False,
            },
        )


def require_zone_capability(
    request: Request,
    auth_result: dict[str, Any],
    *,
    zone_id: str,
    capability: str,
    resource_path: str = "/",
) -> None:
    decision = zone_capability_decision(
        request,
        auth_result,
        zone_id=zone_id,
        capability=capability,
        resource_path=resource_path,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": decision.code, "message": decision.reason, "retryable": False},
        )


def require_runtime_delegation(
    request: Request,
    auth_result: dict[str, Any],
    *,
    delegation_id: str,
    zone_id: str,
    capability: str = "zone.runtime.execute",
    resource_path: str = "/",
) -> VerifiedZoneDelegation:
    """Verify a runtime delegation and return only server-owned references.

    A runtime request may carry a delegation *reference*, but grant/revision/
    epoch values are always loaded from the canonical Nexus store.  A normal
    user may only present their own delegation; an authenticated admin/service
    may register a runtime on behalf of that user without exposing its own
    credential to the runtime.
    """
    if not delegation_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "GRANT_NOT_ACTIVE",
                "message": "runtime delegation is required",
                "retryable": False,
            },
        )
    authz = getattr(request.app.state, "zone_authorization_service", None)
    factory = getattr(request.app.state, "zone_session_factory", None)
    if authz is None or factory is None:
        raise HTTPException(status_code=503, detail="zone authorization unavailable")

    from nexus.storage.models import ZoneDelegationModel

    try:
        with factory() as session:
            row = session.get(ZoneDelegationModel, delegation_id)
            if row is None or row.zone_id != zone_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "RESOURCE_RELATION_DENIED",
                        "message": "delegation does not authorize the runtime zone",
                        "retryable": False,
                    },
                )
            principal = principal_from_auth(auth_result)
            if not auth_result.get("is_admin", False) and (
                principal.subject_type != "user" or principal.subject_id != row.user_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "RESOURCE_RELATION_DENIED",
                        "message": "delegation belongs to another principal",
                        "retryable": False,
                    },
                )
            delegated = authz.verify_delegation(
                session, delegation_id=delegation_id, audience="nexus-api"
            )
            if not delegated:
                unavailable = delegated.code == "MEMBERSHIP_UNAVAILABLE"
                raise HTTPException(
                    status_code=503 if unavailable else 403,
                    detail={
                        "code": delegated.code,
                        "message": delegated.reason,
                        "retryable": unavailable,
                    },
                )
            access = authz.allow(
                session,
                principal=Principal(subject_type="organization", subject_id=row.org_id),
                zone_id=zone_id,
                capability=capability,
                resource_path=resource_path,
            )
            if not access:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": access.code,
                        "message": access.reason,
                        "retryable": False,
                    },
                )
            return VerifiedZoneDelegation(
                delegation_id=row.delegation_id,
                user_id=row.user_id,
                org_id=row.org_id,
                zone_id=row.zone_id,
                grant_id=row.grant_id,
                grant_revision=row.grant_revision,
                authorization_epoch=int(row.epoch),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="zone authorization unavailable") from exc


def zone_capability_decision(
    request: Request,
    auth_result: dict[str, Any],
    *,
    zone_id: str,
    capability: str,
    resource_path: str = "/",
) -> Any:
    if auth_result.get("is_admin", False):
        from nexus.services.zones.authz import Decision

        return Decision(True)
    authz = getattr(request.app.state, "zone_authorization_service", None)
    factory = getattr(request.app.state, "zone_session_factory", None)
    if authz is None or factory is None:
        raise HTTPException(status_code=503, detail="zone authorization unavailable")
    try:
        with factory() as session:
            decision = authz.allow(
                session,
                principal=principal_from_auth(auth_result),
                zone_id=zone_id,
                capability=capability,
                resource_path=resource_path,
            )
            delegation_id = request.headers.get("X-Nexus-Zone-Delegation")
            if not decision and delegation_id:
                delegated = authz.verify_delegation(
                    session,
                    delegation_id=delegation_id,
                    audience="nexus-api",
                )
                if not delegated:
                    decision = delegated
                else:
                    from nexus.storage.models import ZoneDelegationModel

                    row = session.get(ZoneDelegationModel, delegation_id)
                    if (
                        row is not None
                        and principal_from_auth(auth_result).subject_type == "user"
                        and row.user_id == principal_from_auth(auth_result).subject_id
                        and row.zone_id == zone_id
                    ):
                        decision = authz.allow(
                            session,
                            principal=Principal(subject_type="organization", subject_id=row.org_id),
                            zone_id=zone_id,
                            capability=capability,
                            resource_path=resource_path,
                        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="zone authorization unavailable") from exc
    if not decision and decision.code == "MEMBERSHIP_UNAVAILABLE":
        raise HTTPException(
            status_code=503,
            detail={"code": decision.code, "message": decision.reason, "retryable": True},
        )
    return decision
