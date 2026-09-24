"""AuthorizationService — the two-layer allow decision (2C, §11.2 truth table).

Access requires BOTH:

    active ZoneGrant (access-time judgement: pending/revoked/expired all deny)
    ∩ resource-level ReBAC relation

plus current-epoch validation at every execution boundary. Store errors deny
(fail closed) — an authorization service that degrades to allow-all is the
one failure mode this module must make impossible.

Delegations bridge Moss membership: short-lived, bound to
user+org+membership_version+grant/epoch+audience, and their verification
checks the SAME two layers plus the delegation's own freshness — a removed
or downgraded member's next access fails here, not at the runtime.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.services.zones.membership import MembershipUnreachable
from nexus.storage.models import (
    ZoneAuthorizationEpochModel,
    ZoneDelegationModel,
    ZoneGrantModel,
    ZoneModel,
)

DEFAULT_DELEGATION_TTL_S = 900

_CAPABILITY_PERMISSIONS = {
    "zone.data.read": "read",
    "zone.data.export": "read",
    "zone.data.write": "write",
    "zone.runtime.execute": "execute",
    "zone.metadata.manage": "write",
    "zone.grants.manage": "write",
    "zone.lifecycle.delete": "write",
}


class Decision:
    __slots__ = ("allowed", "code", "reason")

    def __init__(self, allowed: bool, *, code: str = "", reason: str = "") -> None:
        self.allowed = allowed
        self.code = code
        self.reason = reason

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class Principal:
    subject_type: str
    subject_id: str
    trust_domain: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"subject_type": self.subject_type, "subject_id": self.subject_id}
        if self.trust_domain is not None:
            out["trust_domain"] = self.trust_domain
        return out


class AuthorizationService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        rebac_check: Callable[[Session, str, str, str, str], bool],
        *,
        membership_check: Callable[[str, str, str], bool] | None = None,
        trusted_issuers: frozenset[str] = frozenset(),
    ) -> None:
        """``rebac_check(session, subject, relation, obj) -> bool`` plugs the
        ReBAC store in; it returns False on store errors as well — this
        service turns that into a denial, never an allow."""
        self._session_factory = session_factory
        self._rebac_check = rebac_check
        self._membership_check = membership_check
        self._trusted_issuers = trusted_issuers

    # ── the two-layer decision ──────────────────────────────────────────────

    def allow(
        self,
        session: Session,
        *,
        principal: Principal,
        zone_id: str,
        capability: str,
        resource_path: str,
    ) -> Decision:
        permission = _CAPABILITY_PERMISSIONS.get(capability)
        if permission is None:
            return Decision(False, code="UNSUPPORTED_CAPABILITY", reason="capability not enabled")
        zone = session.get(ZoneModel, zone_id)
        if zone is not None and zone.canonical_status != "active" and permission == "write":
            return Decision(False, code="ZONE_NOT_ACTIVE", reason="zone is not active")
        grants = (
            session.execute(
                select(ZoneGrantModel).where(
                    ZoneGrantModel.zone_id == zone_id, ZoneGrantModel.status == "active"
                )
            )
            .scalars()
            .all()
        )
        if not grants:
            return Decision(False, code="GRANT_NOT_ACTIVE", reason="no active grant")

        now = datetime.now(UTC)
        matched = None
        for g in grants:
            if not _grant_covers_principal(g, principal):
                continue
            if capability not in (g.capabilities or []):
                continue
            if g.not_before is not None and _aware(g.not_before) > now:
                continue
            if g.expires_at is not None and _aware(g.expires_at) <= now:  # access-time judgement
                continue
            if g.resource_prefixes and not any(
                resource_path == p or resource_path.startswith(p.rstrip("/") + "/")
                for p in g.resource_prefixes
            ):
                continue
            matched = g
            break
        if matched is None:
            return Decision(False, code="GRANT_NOT_ACTIVE", reason="no covering active grant")

        obj = resource_path
        try:
            rebac_allowed = self._rebac_check(
                session,
                f"{principal.subject_type}:{principal.subject_id}",
                permission,
                obj,
                zone_id,
            )
        except Exception:
            rebac_allowed = False
        if not rebac_allowed:
            return Decision(
                False, code="RESOURCE_RELATION_DENIED", reason="grant holds, relation missing"
            )
        return Decision(True)

    # ── epoch validation (execution boundaries) ─────────────────────────────

    def current_epoch(self, session: Session, zone_id: str) -> int | None:
        row = session.get(ZoneAuthorizationEpochModel, zone_id)
        if row is None:
            return None
        return int(row.epoch)

    def epoch_is_current(self, session: Session, zone_id: str, observed_epoch: int) -> bool:
        """Cannot obtain fresh state → deny (§5.5)."""
        current = self.current_epoch(session, zone_id)
        return current is not None and observed_epoch == current

    # ── membership → delegation bridge (§5.4) ────────────────────────────────

    def issue_delegation(
        self,
        session: Session,
        *,
        principal: Principal,
        issuer: Principal | None = None,
        org_id: str,
        membership_version: str,
        zone_id: str,
        audience: str,
        ttl_s: int = DEFAULT_DELEGATION_TTL_S,
        idempotency_key: str | None = None,
    ) -> ZoneDelegationModel:
        """Called ONLY by the trusted Moss issuance service identity; the
        membership itself was validated Moss-side (SSOT stays there)."""
        if issuer is None or (
            issuer.subject_type != "service" or issuer.subject_id not in self._trusted_issuers
        ):
            from nexus.services.zones.service import ServiceError

            raise ServiceError(
                "RESOURCE_RELATION_DENIED",
                "delegations require a trusted Moss issuance service",
                http_status=403,
            )
        if self._membership_check is None:
            from nexus.services.zones.service import ServiceError

            raise ServiceError(
                "RESOURCE_RELATION_DENIED",
                "membership verification is not armed",
                retryable=True,
                http_status=503,
            )
        try:
            membership_active = self._membership_check(
                principal.subject_id, org_id, membership_version
            )
        except MembershipUnreachable as exc:
            from nexus.services.zones.service import ServiceError

            raise ServiceError(
                "MEMBERSHIP_UNAVAILABLE",
                str(exc),
                retryable=True,
                http_status=503,
            ) from exc
        if not membership_active:
            from nexus.services.zones.service import ServiceError

            raise ServiceError(
                "RESOURCE_RELATION_DENIED",
                "Moss membership is not active at the supplied version",
                http_status=403,
            )
        now = datetime.now(UTC)
        grants = (
            session.execute(
                select(ZoneGrantModel)
                .where(
                    ZoneGrantModel.zone_id == zone_id,
                    ZoneGrantModel.status == "active",
                )
                .order_by(ZoneGrantModel.created_at.desc())
            )
            .scalars()
            .all()
        )
        grant = next(
            (
                item
                for item in grants
                if isinstance(item.grantee, dict)
                and item.grantee.get("subject_type") == "organization"
                and item.grantee.get("subject_id") == org_id
                and (item.not_before is None or _aware(item.not_before) <= now)
                and (item.expires_at is None or _aware(item.expires_at) > now)
            ),
            None,
        )
        if grant is None:
            from nexus.services.zones.service import ServiceError

            raise ServiceError(
                "GRANT_NOT_ACTIVE",
                f"org {org_id} has no active grant on {zone_id}",
                http_status=403,
            )
        epoch = self.current_epoch(session, zone_id)
        if epoch is None:
            from nexus.services.zones.service import ServiceError

            raise ServiceError(
                "PROJECTION_FAILED",
                "zone epoch unavailable — denying",
                retryable=True,
                http_status=503,
            )
        delegation_id = (
            "dlg_"
            + hashlib.sha256(
                json.dumps(
                    [issuer.subject_id, principal.subject_id, org_id, zone_id, idempotency_key],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:24]
            if idempotency_key
            else f"dlg_{secrets.token_hex(12)}"
        )
        existing = session.get(ZoneDelegationModel, delegation_id)
        if existing is not None:
            same = (
                existing.user_id == principal.subject_id
                and existing.org_id == org_id
                and existing.membership_version == membership_version
                and existing.zone_id == zone_id
                and existing.audience == audience
            )
            if not same:
                from nexus.services.zones.service import ServiceError

                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "delegation idempotency key was reused with a different request",
                    http_status=409,
                )
            return existing
        delegation = ZoneDelegationModel(
            delegation_id=delegation_id,
            user_id=principal.subject_id,
            org_id=org_id,
            membership_version=membership_version,
            zone_id=zone_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            epoch=epoch,
            audience=audience,
            status="active",
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_s),
        )
        session.add(delegation)
        return delegation

    def verify_delegation(
        self,
        session: Session,
        *,
        delegation_id: str,
        audience: str,
    ) -> Decision:
        d = session.get(ZoneDelegationModel, delegation_id)
        if d is None or d.status != "active":
            return Decision(False, code="GRANT_NOT_ACTIVE", reason="delegation not active")
        if d.audience != audience:
            return Decision(False, code="RESOURCE_RELATION_DENIED", reason="audience mismatch")
        if _aware(d.expires_at) <= datetime.now(UTC):
            return Decision(False, code="GRANT_EXPIRED", reason="delegation expired")
        if self._membership_check is None:
            return Decision(
                False, code="GRANT_REVOKED", reason="membership verification is not armed"
            )
        try:
            membership_active = self._membership_check(d.user_id, d.org_id, d.membership_version)
        except MembershipUnreachable:
            return Decision(
                False, code="MEMBERSHIP_UNAVAILABLE", reason="Moss membership is unavailable"
            )
        except Exception:
            return Decision(
                False, code="MEMBERSHIP_UNAVAILABLE", reason="membership verification failed"
            )
        if not membership_active:
            return Decision(False, code="GRANT_REVOKED", reason="membership is no longer active")
        grant = session.get(ZoneGrantModel, d.grant_id)
        if grant is None or grant.status != "active":
            return Decision(False, code="GRANT_REVOKED", reason="issuing grant no longer active")
        if not self.epoch_is_current(session, d.zone_id, d.epoch):
            return Decision(False, code="GRANT_REVOKED", reason="epoch moved past delegation")
        return Decision(True)

    def revoke_delegation(self, session: Session, delegation_id: str) -> bool:
        d = session.get(ZoneDelegationModel, delegation_id)
        if d is None:
            return False
        d.status = "revoked"
        d.revoked_at = datetime.now(UTC)
        return True


def _aware(value: datetime) -> datetime:
    """SQLite gives naive datetimes back; normalize before comparing."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _grant_covers_principal(  # noqa: SIM103 — layered checks read clearer expanded
    grant: ZoneGrantModel, principal: Principal
) -> bool:
    grantee = (
        grant.grantee if isinstance(grant.grantee, dict) else json.loads(grant.grantee or "{}")
    )
    if grantee.get("subject_id") != principal.subject_id:
        return False
    if grantee.get("subject_type") != principal.subject_type:
        return False
    td = grantee.get("trust_domain")
    if td is not None and td != principal.trust_domain:  # noqa: SIM103
        return False
    return True
