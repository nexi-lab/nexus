"""Add path and session lifecycle to trajectories and playbooks

Revision ID: 62a871bc45de
Revises: 61f540751c4d
Create Date: 2025-10-29 01:20:31.000000

v0.5.0: Add path context and session lifecycle support for trajectories and playbooks.

This migration adds:
- path: Optional path context for filtering (e.g., "/project-a/")
- session_id: Session identifier for temporary items
- expires_at: Expiration timestamp for automatic cleanup

These fields align trajectories and playbooks with the memory model, enabling:
- ID-based access (like memory): `nexus playbook list`
- Optional path filtering: `nexus playbook list --path /project-a/`
- Session lifecycle management for temporary items
- Consistent architecture across all ACE components
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "62a871bc45de"
down_revision = "61f540751c4d"
branch_labels = None
depends_on = None


def upgrade():
    """Add path and session lifecycle fields to trajectories and playbooks."""

    # === Trajectories ===
    # Add path context field
    op.add_column("trajectories", sa.Column("path", sa.Text(), nullable=True))

    # Add session lifecycle fields
    op.add_column("trajectories", sa.Column("session_id", sa.String(36), nullable=True))
    op.add_column("trajectories", sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Add indexes for efficient querying
    op.create_index("idx_traj_path", "trajectories", ["path"])
    op.create_index("idx_traj_session", "trajectories", ["session_id"])
    op.create_index("idx_traj_expires", "trajectories", ["expires_at"])

    # === Playbooks ===
    # Add path context field
    op.add_column("playbooks", sa.Column("path", sa.Text(), nullable=True))

    # Add session lifecycle fields
    op.add_column("playbooks", sa.Column("session_id", sa.String(36), nullable=True))
    op.add_column("playbooks", sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Add indexes for efficient querying
    op.create_index("idx_playbook_path", "playbooks", ["path"])
    op.create_index("idx_playbook_session", "playbooks", ["session_id"])
    op.create_index("idx_playbook_expires", "playbooks", ["expires_at"])


def downgrade():
    """Remove path and session lifecycle fields from trajectories and playbooks."""

    # === Playbooks ===
    # Remove indexes
    op.drop_index("idx_playbook_expires", "playbooks")
    op.drop_index("idx_playbook_session", "playbooks")
    op.drop_index("idx_playbook_path", "playbooks")

    # Remove columns
    op.drop_column("playbooks", "expires_at")
    op.drop_column("playbooks", "session_id")
    op.drop_column("playbooks", "path")

    # === Trajectories ===
    # Remove indexes
    op.drop_index("idx_traj_expires", "trajectories")
    op.drop_index("idx_traj_session", "trajectories")
    op.drop_index("idx_traj_path", "trajectories")

    # Remove columns
    op.drop_column("trajectories", "expires_at")
    op.drop_column("trajectories", "session_id")
    op.drop_column("trajectories", "path")
