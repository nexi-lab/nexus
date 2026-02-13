"""Baseline: create pre-Alembic tables that were never added via migration.

Revision ID: baseline_pre_alembic_tables
Revises: 58d58578fce0
Create Date: 2026-02-13

These 17 tables were created via Base.metadata.create_all() before Alembic
was adopted. Multiple migrations ALTER them (add/drop columns) but none
CREATE them. Without this baseline, `alembic upgrade head` fails on a fresh
database because the ALTER migrations reference tables that don't exist.

Column schemas reflect the state BEFORE any Alembic migration touches them:
- Columns later ADDED by migrations are excluded (migrations will add them).
- Columns later DROPPED by migrations are included (migrations will drop them).

Issue #1296: Migration test harness surfaced this gap.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "baseline_pre_alembic_tables"
down_revision: Union[str, Sequence[str], None] = "58d58578fce0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all pre-Alembic tables with their original schemas."""
    # --- Independent tables (no foreign keys to other missing tables) ---

    op.create_table(
        "zones",
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("zone_id"),
        sa.UniqueConstraint("domain"),
    )

    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(36), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
    )

    op.create_table(
        "memories",
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=True),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("scope", sa.String(50), nullable=False),
        sa.Column("visibility", sa.String(50), nullable=False),
        sa.Column("memory_type", sa.String(50), nullable=True),
        sa.Column("importance", sa.Float(), nullable=True),
        sa.Column("importance_original", sa.Float(), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=True),
        sa.Column("path_key", sa.String(255), nullable=True),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("superseded_by_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # Columns dropped by later migrations (must exist for DROP to succeed)
        sa.Column("group", sa.String(255), nullable=True),
        sa.Column("mode", sa.String(10), nullable=True),
        sa.PrimaryKeyConstraint("memory_id"),
    )

    op.create_table(
        "mount_configs",
        sa.Column("mount_id", sa.String(36), nullable=False),
        sa.Column("mount_point", sa.Text(), nullable=False),
        sa.Column("backend_type", sa.String(50), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("readonly", sa.Integer(), nullable=False),
        sa.Column("backend_config", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.String(255), nullable=True),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("mount_id"),
        sa.UniqueConstraint("mount_point"),
    )

    op.create_table(
        "oauth_credentials",
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("user_email", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(50), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("client_id", sa.String(255), nullable=True),
        sa.Column("token_uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("credential_id"),
        sa.UniqueConstraint("provider", "user_email", "zone_id", name="uq_oauth_credential"),
    )

    op.create_table(
        "operation_log",
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("operation_type", sa.String(50), nullable=False),
        sa.Column("zone_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("new_path", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.String(64), nullable=True),
        sa.Column("metadata_snapshot", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
    )

    op.create_table(
        "entity_registry",
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=False),
        sa.Column("parent_type", sa.String(50), nullable=True),
        sa.Column("parent_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("entity_type", "entity_id"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", sa.String(36), nullable=False),
        sa.Column("path_id", sa.String(36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("chunk_tokens", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.ForeignKeyConstraint(["path_id"], ["file_paths.path_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "audit_checkpoint",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("checkpoint_at", sa.DateTime(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("merkle_root", sa.String(64), nullable=False),
        sa.Column("first_record_id", sa.String(36), nullable=False),
        sa.Column("last_record_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "exchange_audit_log",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("protocol", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("application", sa.String(20), nullable=False),
        sa.Column("buyer_agent_id", sa.String(255), nullable=False),
        sa.Column("seller_agent_id", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("zone_id", sa.String(36), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("metadata_hash", sa.String(64), nullable=True),
        sa.Column("transfer_id", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", "created_at"),
    )

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_sensitive", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "workspace_snapshots",
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("snapshot_number", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # Columns dropped by later migrations (must exist for DROP to succeed)
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    # Indexes created by create_all() from model index=True (dropped by later migrations)
    op.create_index("ix_workspace_snapshots_tenant_id", "workspace_snapshots", ["tenant_id"])
    op.create_index("ix_workspace_snapshots_agent_id", "workspace_snapshots", ["agent_id"])

    # --- Tables with foreign keys to tables above ---

    op.create_table(
        "share_links",
        sa.Column("link_id", sa.String(36), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("permission_level", sa.String(20), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("max_access_count", sa.Integer(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.Column("extra_data", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("link_id"),
    )

    op.create_table(
        "share_link_access_log",
        sa.Column("log_id", sa.String(36), nullable=False),
        sa.Column("link_id", sa.String(36), nullable=False),
        sa.Column("accessed_at", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("success", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(100), nullable=True),
        sa.Column("accessed_by_user_id", sa.String(255), nullable=True),
        sa.Column("accessed_by_zone_id", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("log_id"),
        sa.ForeignKeyConstraint(["link_id"], ["share_links.link_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "workflows",
        sa.Column("workflow_id", sa.String(36), nullable=False),
        sa.Column("zone_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("workflow_id"),
        sa.UniqueConstraint("zone_id", "name", name="uq_zone_workflow_name"),
    )

    op.create_table(
        "workflow_executions",
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("workflow_id", sa.String(36), nullable=False),
        sa.Column("trigger_type", sa.String(100), nullable=False),
        sa.Column("trigger_context", sa.Text(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("actions_completed", sa.Integer(), nullable=False),
        sa.Column("actions_total", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.workflow_id"], ondelete="CASCADE"),
    )

    # Note: oauth_api_keys has FKs to api_keys and users.
    # users table is created by a later migration (add_user_model_tables).
    # oauth_api_keys must be created AFTER both api_keys and users exist.
    # Since users doesn't exist at this point, we skip the FK constraint
    # and let it be enforced at the application level.
    op.create_table(
        "oauth_api_keys",
        sa.Column("key_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("encrypted_key_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key_id"),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.key_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    """Drop all pre-Alembic baseline tables in reverse order."""
    op.drop_table("oauth_api_keys")
    op.drop_table("workflow_executions")
    op.drop_table("workflows")
    op.drop_table("share_link_access_log")
    op.drop_table("share_links")
    op.drop_index("ix_workspace_snapshots_agent_id", table_name="workspace_snapshots")
    op.drop_index("ix_workspace_snapshots_tenant_id", table_name="workspace_snapshots")
    op.drop_table("workspace_snapshots")
    op.drop_table("system_settings")
    op.drop_table("exchange_audit_log")
    op.drop_table("audit_checkpoint")
    op.drop_table("document_chunks")
    op.drop_table("entity_registry")
    op.drop_table("operation_log")
    op.drop_table("oauth_credentials")
    op.drop_table("mount_configs")
    op.drop_table("memories")
    op.drop_table("api_keys")
    op.drop_table("zones")
