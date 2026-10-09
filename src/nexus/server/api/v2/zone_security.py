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


def is_owner_or_admin(auth_result: dict[str, Any], owner_subject_id: str | None) -> bool:
    """Strict owner match (L-1): compares subject_type as well as subject_id.

    Delegation owners are stored as plain user ids and delegations are
    user-scoped by design, so only a real user principal (or an admin)
    matches."""
    if auth_result.get("is_admin", False):
        return True
    return (
        auth_result.get("subject_type") == "user"
        and auth_result.get("subject_id") == owner_subject_id
    )


def owns_operation_or_admin(auth_result: dict[str, Any], principal_id: str | None) -> bool:
    """Operation owners are stored type-qualified ({subject_type}:{subject_id}
    in the idempotency scope), so an agent/service principal whose subject_id
    collides with a victim user's id never matches the user's operations."""
    if auth_result.get("is_admin", False):
        return True
    own = f"{auth_result.get('subject_type') or 'user'}:{auth_result.get('subject_id')}"
    return own == principal_id


def _token_zone_binding(auth_result: dict[str, Any]) -> frozenset[str] | None:
    """The token's zone binding (zone_id / zone_set), if any.

    None means the token carries no binding (users, unscoped admin keys) and
    authorization proceeds on grants alone."""
    zone_set = auth_result.get("zone_set")
    if isinstance(zone_set, (list, tuple, set)) and zone_set:
        return frozenset(str(z) for z in zone_set)
    zone_id = auth_result.get("zone_id")
    if zone_id:
        return frozenset({str(zone_id)})
    return None


def require_zone_capability(
    request: Request,
    auth_result: dict[str, Any],
    *,
    zone_id: str,
    capability: str,
    resource_path: str | None = None,
    delegation_ref: str | None = None,
) -> None:
    decision = zone_capability_decision(
        request,
        auth_result,
        zone_id=zone_id,
        capability=capability,
        resource_path=resource_path,
        delegation_ref=delegation_ref,
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
    resource_path: str | None = None,
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
                session,
                delegation_id=delegation_id,
                audience="nexus-api",
                capability=capability,
                resource_path=resource_path,
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
                resource_path=resource_path or "/",
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
    resource_path: str | None = None,
    delegation_ref: str | None = None,
) -> Any:
    # The API key's zone binding is a hard token boundary (legacy auth_zone):
    # a key minted for zone A never widens to zone B through this surface,
    # whatever grants its subject holds elsewhere — UNLESS the caller presents
    # an explicit delegation for this zone: a delegation is a named, TTL- and
    # prefix-scoped grant for exactly this decision, which is a stronger
    # statement than the token's ambient binding (cross-zone execution, p0
    # §15b). The delegation itself is still verified below.
    explicit_delegation = (delegation_ref or "").strip() or request.headers.get(
        "X-Nexus-Zone-Delegation"
    )
    bound = _token_zone_binding(auth_result)
    if bound is not None and zone_id not in bound and not explicit_delegation:
        from nexus.services.zones.authz import Decision

        return Decision(
            False,
            code="ZONE_OUT_OF_TOKEN_SCOPE",
            reason="token is bound to other zones",
        )
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
                resource_path=resource_path or "/",
            )
            # The runtime sessions surface accepts the delegation reference
            # from body OR header (_runtime_delegation_ref); callers that
            # already resolved it pass it through, the header stays the
            # zero-dependency fallback.
            delegation_id = delegation_ref or request.headers.get("X-Nexus-Zone-Delegation")
            if not decision and delegation_id:
                delegated = authz.verify_delegation(
                    session,
                    delegation_id=delegation_id,
                    audience="nexus-api",
                    capability=capability,
                    resource_path=resource_path,
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
                            resource_path=resource_path or "/",
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
