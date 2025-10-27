"""workspace_snapshots_use_workspace_path

This migration changes WorkspaceSnapshotModel from using tenant_id+agent_id
to using workspace_path for workspace identification.

Before:
- tenant_id (nullable)
- agent_id (required)
- Workspace path constructed: /workspace/{tenant_id}/{agent_id}/

After:
- workspace_path (required)
- Direct path reference to registered workspace

This aligns with the new WorkspaceRegistry system where workspaces are
explicitly registered and referenced by path.

Revision ID: b98c750d8d1a
Revises: eb7fb97cf314
Create Date: 2025-10-25 01:23:35.240105

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b98c750d8d1a"
down_revision: Union[str, Sequence[str], None] = "eb7fb97cf314"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Change workspace_snapshots to use workspace_path instead of tenant_id+agent_id."""

    # Add new workspace_path column (nullable initially)
    op.add_column("workspace_snapshots", sa.Column("workspace_path", sa.Text(), nullable=True))

    # Migrate existing data: build workspace_path from tenant_id + agent_id
    # Format: /workspace/{tenant_id}/{agent_id}/ or /workspace/{agent_id}/ if no tenant
    op.execute("""
        UPDATE workspace_snapshots
        SET workspace_path = CASE
            WHEN tenant_id IS NOT NULL THEN '/workspace/' || tenant_id || '/' || agent_id
            ELSE '/workspace/' || agent_id
        END
    """)

    # Make workspace_path NOT NULL now that data is migrated
    op.alter_column("workspace_snapshots", "workspace_path", nullable=False)

    # Create index on workspace_path
    op.create_index(
        "idx_workspace_snapshots_workspace_path", "workspace_snapshots", ["workspace_path"]
    )

    # Drop old indexes on tenant_id and agent_id
    op.drop_index("ix_workspace_snapshots_tenant_id", table_name="workspace_snapshots")
    op.drop_index("ix_workspace_snapshots_agent_id", table_name="workspace_snapshots")

    # Drop old columns
    op.drop_column("workspace_snapshots", "tenant_id")
    op.drop_column("workspace_snapshots", "agent_id")


def downgrade() -> None:
    """Revert to tenant_id+agent_id based workspace identification."""

    # Add back tenant_id and agent_id columns
    op.add_column("workspace_snapshots", sa.Column("tenant_id", sa.String(255), nullable=True))
    op.add_column("workspace_snapshots", sa.Column("agent_id", sa.String(255), nullable=True))

    # Migrate data back: extract tenant_id and agent_id from workspace_path
    # Format: /workspace/{tenant_id}/{agent_id} or /workspace/{agent_id}
    op.execute("""
        UPDATE workspace_snapshots
        SET
            tenant_id = CASE
                -- If path has 3 segments after split: /, workspace, {tenant}, {agent}
                WHEN (LENGTH(workspace_path) - LENGTH(REPLACE(workspace_path, '/', ''))) >= 3
                THEN SPLIT_PART(workspace_path, '/', 3)
                ELSE NULL
            END,
            agent_id = CASE
                -- If path has 3+ segments: agent_id is 4th segment
                WHEN (LENGTH(workspace_path) - LENGTH(REPLACE(workspace_path, '/', ''))) >= 3
                THEN SPLIT_PART(workspace_path, '/', 4)
                -- Otherwise it's 3rd segment
                ELSE SPLIT_PART(workspace_path, '/', 3)
            END
    """)

    # Make agent_id NOT NULL (tenant_id stays nullable)
    op.alter_column("workspace_snapshots", "agent_id", nullable=False)

    # Recreate indexes
    op.create_index("ix_workspace_snapshots_tenant_id", "workspace_snapshots", ["tenant_id"])
    op.create_index("ix_workspace_snapshots_agent_id", "workspace_snapshots", ["agent_id"])

    # Drop workspace_path index and column
    op.drop_index("idx_workspace_snapshots_workspace_path", table_name="workspace_snapshots")
    op.drop_column("workspace_snapshots", "workspace_path")
