"""Add skill_user_preferences table for agent skill access control

Revision ID: add_skill_prefs
Revises: add_sync_jobs
Create Date: 2025-12-12

Adds skill_user_preferences table to control which skills users grant to their agents.

Features:
- User grants/revokes skill access to specific agents
- Each preference is for a (user, agent, skill) combination
- Tenant isolation for multi-tenancy
- Default: skills are granted (enabled) unless explicitly revoked
- Reason tracking for audit purposes

Example use cases:
- User "alice" revokes "sql-query" skill from agent "chatbot" for safety
- User "bob" grants "code-review" skill only to agent "dev-assistant"
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_skill_prefs"
down_revision: Union[str, Sequence[str], None] = "add_sync_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create skill_user_preferences table
    op.create_table(
        "skill_user_preferences",
        sa.Column("preference_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "agent_id", sa.String(length=255), nullable=False
        ),  # Required: which agent to grant/revoke
        sa.Column("tenant_id", sa.String(length=36), nullable=True),  # Tenant isolation
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("reason", sa.Text(), nullable=True),  # Why was it granted/revoked
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("preference_id"),
    )

    # Indexes for efficient lookups
    # 1. Lookup user preferences for a skill
    op.create_index(
        "idx_skill_pref_user_skill",
        "skill_user_preferences",
        ["user_id", "skill_name", "agent_id"],
        unique=False,
    )

    # 2. Find all preferences for a user (to list disabled skills)
    op.create_index(
        "idx_skill_pref_user",
        "skill_user_preferences",
        ["user_id", "agent_id"],
        unique=False,
    )

    # 3. Tenant-scoped lookup
    op.create_index(
        "idx_skill_pref_tenant",
        "skill_user_preferences",
        ["tenant_id", "user_id"],
        unique=False,
    )

    # 4. Find all users who disabled a specific skill (admin/analytics)
    op.create_index(
        "idx_skill_pref_skill_name",
        "skill_user_preferences",
        ["skill_name", "enabled"],
        unique=False,
    )

    # 5. Unique constraint: one preference per (user, skill, agent) combination
    op.create_index(
        "idx_skill_pref_unique",
        "skill_user_preferences",
        ["user_id", "skill_name", "agent_id"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes
    op.drop_index("idx_skill_pref_unique", table_name="skill_user_preferences")
    op.drop_index("idx_skill_pref_skill_name", table_name="skill_user_preferences")
    op.drop_index("idx_skill_pref_tenant", table_name="skill_user_preferences")
    op.drop_index("idx_skill_pref_user", table_name="skill_user_preferences")
    op.drop_index("idx_skill_pref_user_skill", table_name="skill_user_preferences")

    # Drop table
    op.drop_table("skill_user_preferences")
