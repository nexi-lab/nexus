"""add_trajectory_feedback_for_dynamic_feedback

Dynamic Feedback System for ACE (v0.5.0)

Adds support for dynamic feedback on trajectories:
1. trajectory_feedback table for storing feedback entries
2. Additional fields on trajectories table for feedback tracking

This enables production monitoring, human ratings, A/B test results,
and long-term metrics to improve agent learning.

Revision ID: 61f540751c4d
Revises: 820f721c6e38
Create Date: 2025-10-28 23:26:40.260990

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "61f540751c4d"
down_revision: Union[str, Sequence[str], None] = "820f721c6e38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add trajectory feedback support."""

    # 1. Create trajectory_feedback table
    op.create_table(
        "trajectory_feedback",
        sa.Column("feedback_id", sa.String(36), nullable=False),
        sa.Column("trajectory_id", sa.String(36), nullable=False),
        sa.Column("feedback_type", sa.String(50), nullable=False),
        sa.Column("revised_score", sa.Float(), nullable=True),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
        sa.ForeignKeyConstraint(
            ["trajectory_id"], ["trajectories.trajectory_id"], ondelete="CASCADE"
        ),
    )

    # Create indexes for trajectory_feedback
    op.create_index("idx_feedback_trajectory", "trajectory_feedback", ["trajectory_id"])
    op.create_index("idx_feedback_type", "trajectory_feedback", ["feedback_type"])
    op.create_index("idx_feedback_created", "trajectory_feedback", ["created_at"])

    # 2. Add feedback tracking fields to trajectories table
    with op.batch_alter_table("trajectories", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("feedback_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("effective_score", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("needs_relearning", sa.Integer(), nullable=False, server_default="0")
        )  # Boolean as Integer for SQLite
        batch_op.add_column(
            sa.Column("relearning_priority", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("last_feedback_at", sa.DateTime(), nullable=True))

    # Create index for relearning queue
    op.create_index(
        "idx_traj_relearning", "trajectories", ["needs_relearning", "relearning_priority"]
    )


def downgrade() -> None:
    """Remove trajectory feedback support."""

    # Drop relearning index
    op.drop_index("idx_traj_relearning", table_name="trajectories")

    # Remove fields from trajectories table
    with op.batch_alter_table("trajectories", schema=None) as batch_op:
        batch_op.drop_column("last_feedback_at")
        batch_op.drop_column("relearning_priority")
        batch_op.drop_column("needs_relearning")
        batch_op.drop_column("effective_score")
        batch_op.drop_column("feedback_count")

    # Drop trajectory_feedback table and indexes
    op.drop_index("idx_feedback_created", table_name="trajectory_feedback")
    op.drop_index("idx_feedback_type", table_name="trajectory_feedback")
    op.drop_index("idx_feedback_trajectory", table_name="trajectory_feedback")
    op.drop_table("trajectory_feedback")
