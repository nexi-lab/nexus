"""add_skills_analytics_governance_audit_tables

Revision ID: 928a619dabf4
Revises: a16e1db56def
Create Date: 2025-10-20 00:02:21.309929

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "928a619dabf4"
down_revision: Union[str, Sequence[str], None] = "62a871bc45de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create skill_usage table for analytics
    op.create_table(
        "skill_usage",
        sa.Column("usage_id", sa.String(length=36), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("execution_time", sa.Float(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("usage_id"),
    )
    op.create_index("idx_skill_usage_skill_name", "skill_usage", ["skill_name"], unique=False)
    op.create_index("idx_skill_usage_agent_id", "skill_usage", ["agent_id"], unique=False)
    op.create_index("idx_skill_usage_timestamp", "skill_usage", ["timestamp"], unique=False)
    op.create_index(
        "idx_skill_usage_skill_timestamp", "skill_usage", ["skill_name", "timestamp"], unique=False
    )

    # Create skill_approvals table for governance
    op.create_table(
        "skill_approvals",
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("submitted_by", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),  # pending, approved, rejected
        sa.Column("reviewers", sa.JSON(), nullable=True),  # List of reviewer IDs
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index(
        "idx_skill_approvals_skill_name", "skill_approvals", ["skill_name"], unique=False
    )
    op.create_index("idx_skill_approvals_status", "skill_approvals", ["status"], unique=False)
    op.create_index(
        "idx_skill_approvals_submitted_by", "skill_approvals", ["submitted_by"], unique=False
    )

    # Create skill_audit_log table for audit trails
    op.create_table(
        "skill_audit_log",
        sa.Column("audit_id", sa.String(length=36), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column(
            "action", sa.String(length=50), nullable=False
        ),  # created, executed, forked, published
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column(
            "details", sa.JSON(), nullable=True
        ),  # Additional context (inputs, outputs, etc.)
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index("idx_skill_audit_skill_name", "skill_audit_log", ["skill_name"], unique=False)
    op.create_index("idx_skill_audit_action", "skill_audit_log", ["action"], unique=False)
    op.create_index("idx_skill_audit_agent_id", "skill_audit_log", ["agent_id"], unique=False)
    op.create_index("idx_skill_audit_timestamp", "skill_audit_log", ["timestamp"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Drop audit log table
    op.drop_index("idx_skill_audit_timestamp", table_name="skill_audit_log")
    op.drop_index("idx_skill_audit_agent_id", table_name="skill_audit_log")
    op.drop_index("idx_skill_audit_action", table_name="skill_audit_log")
    op.drop_index("idx_skill_audit_skill_name", table_name="skill_audit_log")
    op.drop_table("skill_audit_log")

    # Drop approvals table
    op.drop_index("idx_skill_approvals_submitted_by", table_name="skill_approvals")
    op.drop_index("idx_skill_approvals_status", table_name="skill_approvals")
    op.drop_index("idx_skill_approvals_skill_name", table_name="skill_approvals")
    op.drop_table("skill_approvals")

    # Drop usage table
    op.drop_index("idx_skill_usage_skill_timestamp", table_name="skill_usage")
    op.drop_index("idx_skill_usage_timestamp", table_name="skill_usage")
    op.drop_index("idx_skill_usage_agent_id", table_name="skill_usage")
    op.drop_index("idx_skill_usage_skill_name", table_name="skill_usage")
    op.drop_table("skill_usage")
