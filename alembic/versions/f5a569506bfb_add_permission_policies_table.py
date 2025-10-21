"""add_permission_policies_table

Revision ID: f5a569506bfb
Revises: 928a619dabf4
Create Date: 2025-10-20 17:44:58.978783

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a569506bfb"
down_revision: Union[str, Sequence[str], None] = "928a619dabf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create permission_policies table
    op.create_table(
        "permission_policies",
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("namespace_pattern", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("default_owner", sa.String(length=255), nullable=False),
        sa.Column("default_group", sa.String(length=255), nullable=False),
        sa.Column("default_mode", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("policy_id"),
    )

    # Create indexes
    op.create_index(
        "idx_permission_policies_namespace",
        "permission_policies",
        ["namespace_pattern"],
        unique=False,
    )
    op.create_index(
        "idx_permission_policies_tenant", "permission_policies", ["tenant_id"], unique=False
    )
    op.create_index(
        "idx_permission_policies_priority", "permission_policies", ["priority"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes
    op.drop_index("idx_permission_policies_priority", table_name="permission_policies")
    op.drop_index("idx_permission_policies_tenant", table_name="permission_policies")
    op.drop_index("idx_permission_policies_namespace", table_name="permission_policies")

    # Drop table
    op.drop_table("permission_policies")
