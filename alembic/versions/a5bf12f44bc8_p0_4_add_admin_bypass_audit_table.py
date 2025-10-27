"""P0-4: Add admin bypass audit table

Revision ID: a5bf12f44bc8
Revises: 4f0aaaec2735
Create Date: 2025-10-24 23:24:36.814670

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5bf12f44bc8"
down_revision: Union[str, Sequence[str], None] = "4f0aaaec2735"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Add admin bypass audit table (P0-4)."""

    # Create admin_bypass_audit table for immutable audit logging
    op.create_table(
        "admin_bypass_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(255), nullable=True, index=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("permission", sa.String(50), nullable=False),
        sa.Column("bypass_type", sa.String(20), nullable=False),  # 'system' or 'admin'
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("capabilities", sa.Text(), nullable=True),  # JSON array of capabilities
        sa.Column("denial_reason", sa.Text(), nullable=True),
    )

    # Create indexes for efficient audit log queries
    op.create_index("idx_audit_timestamp", "admin_bypass_audit", ["timestamp"])
    op.create_index("idx_audit_user_timestamp", "admin_bypass_audit", ["user_id", "timestamp"])
    op.create_index("idx_audit_tenant_timestamp", "admin_bypass_audit", ["tenant_id", "timestamp"])


def downgrade() -> None:
    """Downgrade schema - Remove admin bypass audit table."""

    # Drop indexes
    op.drop_index("idx_audit_tenant_timestamp", table_name="admin_bypass_audit")
    op.drop_index("idx_audit_user_timestamp", table_name="admin_bypass_audit")
    op.drop_index("idx_audit_timestamp", table_name="admin_bypass_audit")

    # Drop table
    op.drop_table("admin_bypass_audit")
