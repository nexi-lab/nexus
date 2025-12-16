"""add subscriptions table for webhook events

Revision ID: add_subscriptions_table
Revises: eb7fb97cf314
Create Date: 2025-01-15

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_subscriptions_table"
down_revision: tuple[str, ...] = ("eb7fb97cf314", "add_inherit_perms")  # Merge heads
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create subscriptions table for webhook event notifications."""
    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(255), nullable=True),
        sa.Column(
            "event_types",
            sa.Text(),
            nullable=False,
            server_default='["file_write", "file_delete", "file_rename"]',
        ),
        sa.Column("patterns", sa.Text(), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("custom_metadata", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_delivery_at", sa.DateTime(), nullable=True),
        sa.Column("last_delivery_status", sa.String(50), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), nullable=True),
    )

    # Create indexes
    op.create_index("idx_subscriptions_tenant", "subscriptions", ["tenant_id"])
    op.create_index("idx_subscriptions_enabled", "subscriptions", ["enabled"])
    op.create_index("idx_subscriptions_url", "subscriptions", ["url"])


def downgrade() -> None:
    """Drop subscriptions table."""
    op.drop_index("idx_subscriptions_url", table_name="subscriptions")
    op.drop_index("idx_subscriptions_enabled", table_name="subscriptions")
    op.drop_index("idx_subscriptions_tenant", table_name="subscriptions")
    op.drop_table("subscriptions")
