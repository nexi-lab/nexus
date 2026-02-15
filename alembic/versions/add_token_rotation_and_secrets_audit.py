"""feat(#997): Add token rotation history and secrets audit log tables

Revision ID: add_token_rotation_secrets_audit
Revises: add_memory_evolution_fields
Create Date: 2026-02-15

Adds:
1. Three new columns on oauth_credentials for RFC 9700 token rotation:
   - token_family_id (UUID, indexed)
   - rotation_counter (integer, default 0)
   - refresh_token_hash (SHA-256 hex digest)
2. refresh_token_history table for reuse detection
3. secrets_audit_log table for immutable credential audit trail

All additive — no existing data is modified.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_token_rotation_secrets_audit"
down_revision: Union[str, Sequence[str], None] = "add_memory_evolution_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add token rotation columns, refresh_token_history, and secrets_audit_log."""

    # --- 1. oauth_credentials: token rotation columns ---
    op.add_column(
        "oauth_credentials",
        sa.Column("token_family_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "oauth_credentials",
        sa.Column("rotation_counter", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "oauth_credentials",
        sa.Column("refresh_token_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "idx_oauth_token_family",
        "oauth_credentials",
        ["token_family_id"],
        unique=False,
    )

    # --- 2. refresh_token_history table ---
    op.create_table(
        "refresh_token_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_family_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False),
        sa.Column("rotation_counter", sa.Integer(), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_rth_family_hash",
        "refresh_token_history",
        ["token_family_id", "refresh_token_hash"],
    )
    op.create_index(
        "idx_rth_family_rotated_at",
        "refresh_token_history",
        ["token_family_id", "rotated_at"],
    )
    op.create_index("idx_rth_credential", "refresh_token_history", ["credential_id"])
    op.create_index("idx_rth_rotated_at", "refresh_token_history", ["rotated_at"])
    op.create_index("idx_rth_zone", "refresh_token_history", ["zone_id"])

    # --- 3. secrets_audit_log table ---
    op.create_table(
        "secrets_audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("credential_id", sa.String(36), nullable=True),
        sa.Column("token_family_id", sa.String(36), nullable=True),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("metadata_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "idx_secrets_audit_actor_created",
        "secrets_audit_log",
        ["actor_id", "created_at"],
    )
    op.create_index(
        "idx_secrets_audit_zone_created",
        "secrets_audit_log",
        ["zone_id", "created_at"],
    )
    op.create_index(
        "idx_secrets_audit_event_type",
        "secrets_audit_log",
        ["event_type"],
    )
    op.create_index(
        "idx_secrets_audit_credential",
        "secrets_audit_log",
        ["credential_id"],
    )
    op.create_index(
        "idx_secrets_audit_family",
        "secrets_audit_log",
        ["token_family_id"],
    )

    # --- 4. PostgreSQL immutability triggers (no-op on SQLite) ---
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION prevent_secrets_audit_log_modification()
                RETURNS TRIGGER AS $$
                BEGIN
                    RAISE EXCEPTION
                        'secrets_audit_log records are immutable: % not allowed',
                        TG_OP;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER trg_secrets_audit_log_no_update
                BEFORE UPDATE ON secrets_audit_log
                FOR EACH ROW
                EXECUTE FUNCTION prevent_secrets_audit_log_modification();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER trg_secrets_audit_log_no_delete
                BEFORE DELETE ON secrets_audit_log
                FOR EACH ROW
                EXECUTE FUNCTION prevent_secrets_audit_log_modification();
                """
            )
        )


def downgrade() -> None:
    """Remove token rotation columns, refresh_token_history, and secrets_audit_log."""
    # Drop PostgreSQL triggers + function BEFORE dropping the table
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_secrets_audit_log_no_delete ON secrets_audit_log")
        )
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_secrets_audit_log_no_update ON secrets_audit_log")
        )
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS prevent_secrets_audit_log_modification()")
        )

    # secrets_audit_log
    op.drop_index("idx_secrets_audit_family", table_name="secrets_audit_log")
    op.drop_index("idx_secrets_audit_credential", table_name="secrets_audit_log")
    op.drop_index("idx_secrets_audit_event_type", table_name="secrets_audit_log")
    op.drop_index("idx_secrets_audit_zone_created", table_name="secrets_audit_log")
    op.drop_index("idx_secrets_audit_actor_created", table_name="secrets_audit_log")
    op.drop_table("secrets_audit_log")

    # refresh_token_history
    op.drop_index("idx_rth_zone", table_name="refresh_token_history")
    op.drop_index("idx_rth_rotated_at", table_name="refresh_token_history")
    op.drop_index("idx_rth_credential", table_name="refresh_token_history")
    op.drop_index("idx_rth_family_rotated_at", table_name="refresh_token_history")
    op.drop_index("idx_rth_family_hash", table_name="refresh_token_history")
    op.drop_table("refresh_token_history")

    # oauth_credentials columns
    op.drop_index("idx_oauth_token_family", table_name="oauth_credentials")
    op.drop_column("oauth_credentials", "refresh_token_hash")
    op.drop_column("oauth_credentials", "rotation_counter")
    op.drop_column("oauth_credentials", "token_family_id")
