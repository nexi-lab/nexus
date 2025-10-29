"""ace_foundation_v0_5_0

ACE (Agentic Context Engineering) Foundation - v0.5.0

This migration adds the database foundation for ACE integration:
1. Trajectory tracking table
2. Playbook storage table
3. Extended memories table with ACE relationships
4. Agent fields (user_id, agent_id) in workspace_configs and memory_configs

Note: agent_type field removed - lifecycle managed via API key TTL instead

Revision ID: a5f83e7d53d7
Revises: 217a7e641338
Create Date: 2025-10-28 15:08:03.600369

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5f83e7d53d7"
down_revision: Union[str, Sequence[str], None] = "217a7e641338"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ACE foundation tables and fields."""

    # 1. Add ACE relationship fields to memories table
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("trajectory_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("playbook_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("consolidated_from", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("consolidation_version", sa.Integer(), nullable=False, server_default="0")
        )

    # 2. Add agent fields to workspace_configs
    with op.batch_alter_table("workspace_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("agent_id", sa.String(255), nullable=True))

    # 3. Add agent fields to memory_configs
    with op.batch_alter_table("memory_configs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("agent_id", sa.String(255), nullable=True))

    # 4. Create trajectories table
    op.create_table(
        "trajectories",
        sa.Column("trajectory_id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("tenant_id", sa.String(255), nullable=True),
        sa.Column("task_description", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(50), nullable=True),
        sa.Column("trace_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("success_score", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("parent_trajectory_id", sa.String(36), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("trajectory_id"),
        sa.ForeignKeyConstraint(
            ["parent_trajectory_id"], ["trajectories.trajectory_id"], ondelete="SET NULL"
        ),
    )

    # Create indexes for trajectories
    op.create_index("idx_traj_user", "trajectories", ["user_id"])
    op.create_index("idx_traj_agent", "trajectories", ["agent_id"])
    op.create_index("idx_traj_tenant", "trajectories", ["tenant_id"])
    op.create_index("idx_traj_status", "trajectories", ["status"])
    op.create_index("idx_traj_task_type", "trajectories", ["task_type"])
    op.create_index("idx_traj_completed", "trajectories", ["completed_at"])

    # 5. Create playbooks table
    op.create_table(
        "playbooks",
        sa.Column("playbook_id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("tenant_id", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("avg_improvement", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("scope", sa.String(50), nullable=False, server_default="'agent'"),
        sa.Column("visibility", sa.String(50), nullable=False, server_default="'private'"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("playbook_id"),
        sa.UniqueConstraint("agent_id", "name", "version", name="uq_playbook_agent_name_version"),
    )

    # Create indexes for playbooks
    op.create_index("idx_playbook_user", "playbooks", ["user_id"])
    op.create_index("idx_playbook_agent", "playbooks", ["agent_id"])
    op.create_index("idx_playbook_tenant", "playbooks", ["tenant_id"])
    op.create_index("idx_playbook_name", "playbooks", ["name"])
    op.create_index("idx_playbook_scope", "playbooks", ["scope"])


def downgrade() -> None:
    """Remove ACE foundation tables and fields."""

    # Drop playbooks table and indexes
    op.drop_index("idx_playbook_scope", table_name="playbooks")
    op.drop_index("idx_playbook_name", table_name="playbooks")
    op.drop_index("idx_playbook_tenant", table_name="playbooks")
    op.drop_index("idx_playbook_agent", table_name="playbooks")
    op.drop_index("idx_playbook_user", table_name="playbooks")
    op.drop_table("playbooks")

    # Drop trajectories table and indexes
    op.drop_index("idx_traj_completed", table_name="trajectories")
    op.drop_index("idx_traj_task_type", table_name="trajectories")
    op.drop_index("idx_traj_status", table_name="trajectories")
    op.drop_index("idx_traj_tenant", table_name="trajectories")
    op.drop_index("idx_traj_agent", table_name="trajectories")
    op.drop_index("idx_traj_user", table_name="trajectories")
    op.drop_table("trajectories")

    # Remove agent fields from memory_configs
    with op.batch_alter_table("memory_configs", schema=None) as batch_op:
        batch_op.drop_column("agent_id")
        batch_op.drop_column("user_id")

    # Remove agent fields from workspace_configs
    with op.batch_alter_table("workspace_configs", schema=None) as batch_op:
        batch_op.drop_column("agent_id")
        batch_op.drop_column("user_id")

    # Remove ACE fields from memories
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_column("consolidation_version")
        batch_op.drop_column("consolidated_from")
        batch_op.drop_column("playbook_id")
        batch_op.drop_column("trajectory_id")
