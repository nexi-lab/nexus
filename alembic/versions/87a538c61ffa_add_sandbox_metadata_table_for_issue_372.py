"""add_sandbox_metadata_table_for_issue_372

Revision ID: 87a538c61ffa
Revises: 196bdb53b12d
Create Date: 2025-11-02 15:24:23.354305

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "87a538c61ffa"
down_revision: Union[str, Sequence[str], None] = "196bdb53b12d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create sandbox_metadata table
    op.create_table(
        "sandbox_metadata",
        sa.Column("sandbox_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False, server_default="e2b"),
        sa.Column("template_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_active_at", sa.DateTime(), nullable=False),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("ttl_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("auto_created", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("sandbox_id"),
        sa.UniqueConstraint("user_id", "name", name="uq_sandbox_user_name"),
    )

    # Create indexes
    op.create_index("idx_sandbox_user", "sandbox_metadata", ["user_id"])
    op.create_index("idx_sandbox_agent", "sandbox_metadata", ["agent_id"])
    op.create_index("idx_sandbox_tenant", "sandbox_metadata", ["tenant_id"])
    op.create_index("idx_sandbox_status", "sandbox_metadata", ["status"])
    op.create_index("idx_sandbox_expires", "sandbox_metadata", ["expires_at"])
    op.create_index("idx_sandbox_created", "sandbox_metadata", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_sandbox_created", table_name="sandbox_metadata")
    op.drop_index("idx_sandbox_expires", table_name="sandbox_metadata")
    op.drop_index("idx_sandbox_status", table_name="sandbox_metadata")
    op.drop_index("idx_sandbox_tenant", table_name="sandbox_metadata")
    op.drop_index("idx_sandbox_agent", table_name="sandbox_metadata")
    op.drop_index("idx_sandbox_user", table_name="sandbox_metadata")
    op.drop_table("sandbox_metadata")
