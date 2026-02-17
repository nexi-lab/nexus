"""feat(#1315): Add context_branches table for workspace branching

Revision ID: add_context_branches
Revises: merge_token_rotation_delivered
Create Date: 2026-02-16

Adds the context_branches table for git-like workspace branching:
- Named branches with head_snapshot_id pointers
- Fork metadata (parent_branch, fork_point_id)
- Optimistic concurrency via pointer_version counter
- Merge audit (merged_into_branch, merge_snapshot_id)
- Current branch tracking (is_current boolean)

All additive — no existing data is modified.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_context_branches"
down_revision: Union[str, Sequence[str], None] = "merge_token_rotation_delivered"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add context_branches table."""
    op.create_table(
        "context_branches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="root"),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("branch_name", sa.String(255), nullable=False),
        sa.Column("head_snapshot_id", sa.String(36), nullable=True),
        sa.Column("parent_branch", sa.String(255), nullable=True),
        sa.Column("fork_point_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pointer_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("merged_into_branch", sa.String(255), nullable=True),
        sa.Column("merge_snapshot_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("zone_id", "workspace_path", "branch_name", name="uq_context_branch"),
    )
    op.create_index("ix_ctx_branch_zone_ws", "context_branches", ["zone_id", "workspace_path"])
    op.create_index("ix_ctx_branch_status", "context_branches", ["status"])


def downgrade() -> None:
    """Remove context_branches table."""
    op.drop_index("ix_ctx_branch_status", table_name="context_branches")
    op.drop_index("ix_ctx_branch_zone_ws", table_name="context_branches")
    op.drop_table("context_branches")
