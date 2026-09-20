"""Canonical persistence for the zone-v1 product surface (2B).

What lives here and why:

- ``zone_grants`` is the append-history canonical store for the §4.6 business
  fact. api_key_zones, ReBAC tuples, delegations and caches are PROJECTIONS of
  it (and are marked as such where they live); nothing in this module is a
  projection.
- ``zone_operations`` carries idempotency (scope+key+request hash), worker
  leasing (owner/expires/generation/fence) and runtime receipts. A stale
  writer is fenced by generation, not by hope.
- the two outbox tables are re-entrant, lease-takeover-able and fenced; SQL
  never pretends an external runtime effect rolled back — the receipt says
  what actually happened.
- ``rebac_relation_sources`` separates grant-derived edges (carrying
  ``source_grant_id``) from independent authoritative relations, so revoking
  one grant can never delete an edge another grant or a human still owns.
- ``zone_authorization_epochs`` / ``zone_delegations`` back the fail-closed
  authorization model (revoked fact + epoch + invalidation commit atomically).

Worker/reconciler and public mutation stay disabled until 2C arms them
(B3 item 10): schema presence grants no capability.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base

GRANTEE_JSON = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ZoneGrantModel(Base):
    """§4.6 ZoneGrant — canonical append-history, never rewritten in place."""

    __tablename__ = "zone_grants"
    __table_args__ = (
        # Idempotent creation anchors on the source identity, never on
        # tuple-matching "similar" grants.
        UniqueConstraint("source_type", "source_id", name="uq_zone_grant_source"),
        Index("ix_zone_grants_zone_status", "zone_id", "status"),
        Index("ix_zone_grants_grantee", "grantee", postgresql_using="gin"),
        Index("ix_zone_grants_expiry", "expires_at"),
    )

    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("zones.zone_id", ondelete="RESTRICT"), nullable=False
    )
    grantee: Mapped[dict] = mapped_column(GRANTEE_JSON, nullable=False)
    capabilities: Mapped[list] = mapped_column(JSON, nullable=False)
    resource_prefixes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_by: Mapped[dict] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ZoneOperationModel(Base):
    """§4.9 ZoneOperation with idempotency, leasing and fencing."""

    __tablename__ = "zone_operations"
    __table_args__ = (
        # scope = principal + action + target; the same key must carry the
        # same canonical request hash or it is an IDEMPOTENCY_CONFLICT.
        UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_zone_operation_idem"),
        Index("ix_zone_operations_zone_state", "zone_id", "state"),
        Index("ix_zone_operations_lease", "lease_expires_at"),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("zones.zone_id", ondelete="RESTRICT"), nullable=True
    )
    grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ZoneMountModel(Base):
    __tablename__ = "zone_mounts"
    __table_args__ = (
        UniqueConstraint("parent_zone_id", "target_zone_id", "path", name="uq_zone_mount"),
    )

    mount_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_zone_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("zones.zone_id", ondelete="RESTRICT"), nullable=False
    )
    target_zone_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("zones.zone_id", ondelete="RESTRICT"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    runtime_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ZoneRuntimeOutboxModel(Base):
    """Runtime-effect outbox: re-entrant, lease takeover, stale-writer fence."""

    __tablename__ = "zone_runtime_outbox"
    __table_args__ = (Index("ix_zone_runtime_outbox_pending", "processed_at", "next_retry_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ZoneGrantProjectionOutboxModel(Base):
    """Projection outbox for grant issuance/revoke effects (fail-closed)."""

    __tablename__ = "zone_grant_projection_outbox"
    __table_args__ = (
        Index("ix_zone_grant_projection_outbox_pending", "processed_at", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ZoneAuthorizationEpochModel(Base):
    """Per-zone monotonic epoch; revoke advances it atomically with the fact."""

    __tablename__ = "zone_authorization_epochs"

    zone_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("zones.zone_id", ondelete="RESTRICT"), primary_key=True
    )
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    advanced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ZoneDelegationModel(Base):
    """PROJECTION: short-lived user delegations bridged from Moss membership.

    Membership SSOT stays in Moss; this stores only issuance/verification
    state bound to user+org+membership_version+grant/epoch+audience.
    """

    __tablename__ = "zone_delegations"
    __table_args__ = (
        Index("ix_zone_delegations_user_org", "user_id", "org_id"),
        Index("ix_zone_delegations_expiry", "expires_at"),
    )

    delegation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_version: Mapped[str] = mapped_column(String(64), nullable=False)
    zone_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("zones.zone_id", ondelete="RESTRICT"), nullable=False
    )
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    grant_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audience: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RebacRelationSourceModel(Base):
    """Provenance ledger separating grant-derived edges from authoritative ones.

    A row with ``source_grant_id`` is a projection of that grant and dies with
    it; a row with NULL is an independent authoritative/manual relation that no
    grant revoke may touch. Overlapping grants each carry their own row.
    """

    __tablename__ = "rebac_relation_sources"
    __table_args__ = (
        UniqueConstraint(
            "subject", "relation", "object", "source_grant_id", name="uq_rebac_rel_src"
        ),
        Index("ix_rebac_rel_src_grant", "source_grant_id"),
        Index("ix_rebac_rel_src_tuple", "subject", "relation", "object"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    relation: Mapped[str] = mapped_column(String(128), nullable=False)
    object: Mapped[str] = mapped_column(String(255), nullable=False)
    source_grant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
