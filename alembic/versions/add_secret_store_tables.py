"""feat: Add secret_store and secret_store_versions tables

Revision ID: add_secret_store_tables
Revises: 2674e0e3f70d
Create Date: 2026-04-07

Creates the core tables for the secrets store feature:
1. secret_store - metadata table for secrets (namespace, key, subject isolation)
2. secret_store_versions - encrypted secret values with version history
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "add_secret_store_tables"
down_revision: Union[str, Sequence[str], None] = "2674e0e3f70d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "secret_store",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=True),
        sa.Column("subject_type", sa.String(20), nullable=True, server_default="user"),
        sa.UniqueConstraint("namespace", "key", name="uq_secret_store_namespace_key"),
    )
    op.create_index("idx_secret_store_namespace", "secret_store", ["namespace"])
    op.create_index("idx_secret_store_deleted_at", "secret_store", ["deleted_at"])
    op.create_index("idx_secret_store_subject", "secret_store", ["subject_id"])

    op.create_table(
        "secret_store_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("secret_id", sa.String(36), sa.ForeignKey("secret_store.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("secret_id", "version", name="uq_secret_store_version_secret_version"),
    )
    op.create_index("idx_secret_store_versions_secret_id", "secret_store_versions", ["secret_id"])


def downgrade() -> None:
    op.drop_index("idx_secret_store_versions_secret_id", table_name="secret_store_versions")
    op.drop_table("secret_store_versions")
    op.drop_index("idx_secret_store_subject", table_name="secret_store")
    op.drop_index("idx_secret_store_deleted_at", table_name="secret_store")
    op.drop_index("idx_secret_store_namespace", table_name="secret_store")
    op.drop_table("secret_store")
