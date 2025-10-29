"""add_session_support_v0_5_0

Adds session management for Nexus (v0.5.0)

Changes:
1. Create user_sessions table for session tracking
2. Add scope/session_id/expires_at to workspace_configs
3. Add scope/session_id/expires_at to memory_configs
4. Add session_id/expires_at to memories table

Revision ID: 3c80651b81c6
Revises: a5f83e7d53d7
Create Date: 2025-10-28 15:53:39.380302

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c80651b81c6"
down_revision: Union[str, Sequence[str], None] = "a5f83e7d53d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add session support."""

    # 1. Create user_sessions table
    op.create_table(
        "user_sessions",
        sa.Column("session_id", sa.String(36), nullable=False, primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("tenant_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "last_activity",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
    )

    # Create indexes for user_sessions
    op.create_index("idx_session_user", "user_sessions", ["user_id"])
    op.create_index("idx_session_agent", "user_sessions", ["agent_id"])
    op.create_index("idx_session_expires", "user_sessions", ["expires_at"])
    op.create_index("idx_session_created", "user_sessions", ["created_at"])

    # 2. Add session fields to workspace_configs
    with op.batch_alter_table("workspace_configs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("scope", sa.String(20), nullable=False, server_default="persistent")
        )
        batch_op.add_column(sa.Column("session_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Create indexes for workspace_configs session fields
    op.create_index("idx_workspace_configs_user", "workspace_configs", ["user_id"])
    op.create_index("idx_workspace_configs_session", "workspace_configs", ["session_id"])
    op.create_index("idx_workspace_configs_expires", "workspace_configs", ["expires_at"])

    # 3. Add session fields to memory_configs
    with op.batch_alter_table("memory_configs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("scope", sa.String(20), nullable=False, server_default="persistent")
        )
        batch_op.add_column(sa.Column("session_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Create indexes for memory_configs session fields
    op.create_index("idx_memory_configs_user", "memory_configs", ["user_id"])
    op.create_index("idx_memory_configs_session", "memory_configs", ["session_id"])
    op.create_index("idx_memory_configs_expires", "memory_configs", ["expires_at"])

    # 4. Add session fields to memories table
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Create indexes for memories session fields
    op.create_index("idx_memory_session", "memories", ["session_id"])
    op.create_index("idx_memory_expires", "memories", ["expires_at"])


def downgrade() -> None:
    """Remove session support."""

    # Remove indexes from memories
    op.drop_index("idx_memory_expires", table_name="memories")
    op.drop_index("idx_memory_session", table_name="memories")

    # Remove session fields from memories
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_column("expires_at")
        batch_op.drop_column("session_id")

    # Remove indexes from memory_configs
    op.drop_index("idx_memory_configs_expires", table_name="memory_configs")
    op.drop_index("idx_memory_configs_session", table_name="memory_configs")
    op.drop_index("idx_memory_configs_user", table_name="memory_configs")

    # Remove session fields from memory_configs
    with op.batch_alter_table("memory_configs", schema=None) as batch_op:
        batch_op.drop_column("expires_at")
        batch_op.drop_column("session_id")
        batch_op.drop_column("scope")

    # Remove indexes from workspace_configs
    op.drop_index("idx_workspace_configs_expires", table_name="workspace_configs")
    op.drop_index("idx_workspace_configs_session", table_name="workspace_configs")
    op.drop_index("idx_workspace_configs_user", table_name="workspace_configs")

    # Remove session fields from workspace_configs
    with op.batch_alter_table("workspace_configs", schema=None) as batch_op:
        batch_op.drop_column("expires_at")
        batch_op.drop_column("session_id")
        batch_op.drop_column("scope")

    # Drop indexes for user_sessions
    op.drop_index("idx_session_created", table_name="user_sessions")
    op.drop_index("idx_session_expires", table_name="user_sessions")
    op.drop_index("idx_session_agent", table_name="user_sessions")
    op.drop_index("idx_session_user", table_name="user_sessions")

    # Drop user_sessions table
    op.drop_table("user_sessions")
