"""add_zone_v1_canonical_storage

2B NEXUS-STORAGE: canonical persistence for the zone-v1 product surface.

Expand-only — nothing existing is dropped or narrowed:

- ``zones`` gains the canonical product columns (display_name, canonical
  status/revision, created_by principal, placement/trust, runtime observed
  receipt/health). Legacy name/phase/settings stay; mapping legacy rows is a
  later, explicitly gated migration (see storage/zone_migration.py — Active
  needs runtime read-back before it can become active, so nothing is
  backfilled here).
- eight new tables: zone_grants (append-history), zone_operations
  (idempotency/lease/fence), zone_mounts, the two outboxes (re-entrant,
  lease takeover, stale-writer fence), zone_authorization_epochs,
  zone_delegations (projection) and rebac_relation_sources (grant-derived
  edges carry source_grant_id; NULL means an independent authoritative
  relation).

Worker/reconciler and public mutation stay disabled until 2C arms them:
schema presence grants no capability.

Revision ID: add_zone_v1_canonical_storage
Revises: oplog_snapshot_hash_text
Create Date: 2026-09-18

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_zone_v1_canonical_storage"
down_revision: Union[str, Sequence[str], None] = "oplog_snapshot_hash_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GRANTEE_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # zones: canonical columns, all nullable (no legacy row is promoted here).
    with op.batch_alter_table("zones", schema=None) as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("canonical_status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("canonical_revision", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("created_by", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("placement_location", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("placement_data_domain", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("trust_domain", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("placement_region", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("replication_policy_ref", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("labels", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("runtime_observed_receipt", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("runtime_health", sa.String(length=16), nullable=True))
        batch_op.add_column(
            sa.Column("runtime_observed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index("idx_zones_canonical_status", ["canonical_status"])

    op.create_table(
        "zone_grants",
        sa.Column("grant_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "zone_id",
            sa.String(length=64),
            sa.ForeignKey("zones.zone_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("grantee", GRANTEE_JSON, nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("resource_prefixes", sa.JSON(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("issued_by", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.JSON(), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("source_type", "source_id", name="uq_zone_grant_source"),
    )
    op.create_index("ix_zone_grants_zone_status", "zone_grants", ["zone_id", "status"])
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "ix_zone_grants_grantee", "zone_grants", ["grantee"], postgresql_using="gin"
        )
    else:
        op.create_index("ix_zone_grants_grantee", "zone_grants", ["grantee"])
    op.create_index("ix_zone_grants_expiry", "zone_grants", ["expires_at"])

    op.create_table(
        "zone_operations",
        sa.Column("operation_id", sa.String(length=64), primary_key=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "zone_id",
            sa.String(length=64),
            sa.ForeignKey("zones.zone_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("grant_id", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("idempotency_scope", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("receipt", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_zone_operation_idem"),
    )
    op.create_index("ix_zone_operations_zone_state", "zone_operations", ["zone_id", "state"])
    op.create_index("ix_zone_operations_lease", "zone_operations", ["lease_expires_at"])

    op.create_table(
        "zone_mounts",
        sa.Column("mount_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "parent_zone_id",
            sa.String(length=64),
            sa.ForeignKey("zones.zone_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_zone_id",
            sa.String(length=64),
            sa.ForeignKey("zones.zone_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("desired_state", sa.String(length=16), nullable=False),
        sa.Column("observed_state", sa.String(length=16), nullable=True),
        sa.Column("runtime_revision", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("parent_zone_id", "target_zone_id", "path", name="uq_zone_mount"),
    )

    op.create_table(
        "zone_runtime_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_zone_runtime_outbox_pending", "zone_runtime_outbox", ["processed_at", "next_retry_at"]
    )

    op.create_table(
        "zone_grant_projection_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("grant_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_zone_grant_projection_outbox_pending",
        "zone_grant_projection_outbox",
        ["processed_at", "next_retry_at"],
    )

    op.create_table(
        "zone_authorization_epochs",
        sa.Column(
            "zone_id",
            sa.String(length=64),
            sa.ForeignKey("zones.zone_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("advanced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
    )

    op.create_table(
        "zone_delegations",
        sa.Column("delegation_id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("membership_version", sa.String(length=64), nullable=False),
        sa.Column(
            "zone_id",
            sa.String(length=64),
            sa.ForeignKey("zones.zone_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("grant_id", sa.String(length=64), nullable=False),
        sa.Column("grant_revision", sa.String(length=64), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("audience", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_zone_delegations_user_org", "zone_delegations", ["user_id", "org_id"])
    op.create_index("ix_zone_delegations_expiry", "zone_delegations", ["expires_at"])

    op.create_table(
        "rebac_relation_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("relation", sa.String(length=128), nullable=False),
        sa.Column("object", sa.String(length=255), nullable=False),
        sa.Column("source_grant_id", sa.String(length=64), nullable=True),
        sa.Column("reference_state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "subject", "relation", "object", "source_grant_id", name="uq_rebac_rel_src"
        ),
    )
    op.create_index("ix_rebac_rel_src_grant", "rebac_relation_sources", ["source_grant_id"])
    op.create_index(
        "ix_rebac_rel_src_tuple", "rebac_relation_sources", ["subject", "relation", "object"]
    )


def downgrade() -> None:
    """Expand-only policy: downgrade is for local development rollback only."""
    op.drop_table("rebac_relation_sources")
    op.drop_table("zone_delegations")
    op.drop_table("zone_authorization_epochs")
    op.drop_index(
        "ix_zone_grant_projection_outbox_pending", table_name="zone_grant_projection_outbox"
    )
    op.drop_table("zone_grant_projection_outbox")
    op.drop_index("ix_zone_runtime_outbox_pending", table_name="zone_runtime_outbox")
    op.drop_table("zone_runtime_outbox")
    op.drop_table("zone_mounts")
    op.drop_index("ix_zone_operations_lease", table_name="zone_operations")
    op.drop_index("ix_zone_operations_zone_state", table_name="zone_operations")
    op.drop_table("zone_operations")
    op.drop_index("ix_zone_grants_expiry", table_name="zone_grants")
    op.drop_index("ix_zone_grants_grantee", table_name="zone_grants")
    op.drop_index("ix_zone_grants_zone_status", table_name="zone_grants")
    op.drop_table("zone_grants")
    with op.batch_alter_table("zones", schema=None) as batch_op:
        batch_op.drop_index("idx_zones_canonical_status")
        batch_op.drop_column("runtime_observed_at")
        batch_op.drop_column("runtime_health")
        batch_op.drop_column("runtime_observed_receipt")
        batch_op.drop_column("labels")
        batch_op.drop_column("replication_policy_ref")
        batch_op.drop_column("placement_region")
        batch_op.drop_column("trust_domain")
        batch_op.drop_column("placement_data_domain")
        batch_op.drop_column("placement_location")
        batch_op.drop_column("created_by")
        batch_op.drop_column("canonical_revision")
        batch_op.drop_column("canonical_status")
        batch_op.drop_column("display_name")
