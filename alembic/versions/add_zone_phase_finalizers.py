"""Add phase and finalizers columns to zones, drop is_active.

Revision ID: add_zone_phase_finalizers
Revises: None
Create Date: 2026-02-19

Zone lifecycle finalizer protocol (Issue #2061).
Replaces the boolean ``is_active`` flag with a richer ``phase`` column
(Active / Terminating / Terminated) and a ``finalizers`` JSON array
that tracks outstanding cleanup work during zone deprovisioning.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_zone_phase_finalizers"
down_revision: Union[str, Sequence[str], None] = "101ea1909580"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add phase + finalizers columns, migrate data, drop is_active."""
    # Step 1: Add new columns with defaults
    op.add_column(
        "zones",
        sa.Column("phase", sa.String(12), nullable=False, server_default="Active"),
    )
    op.add_column(
        "zones",
        sa.Column("finalizers", sa.Text(), nullable=False, server_default="[]"),
    )

    # Step 2: Migrate data — map is_active to phase
    op.execute(
        "UPDATE zones SET phase = CASE WHEN is_active = 1 THEN 'Active' ELSE 'Terminated' END"
    )

    # Step 3: Drop old column and index
    op.drop_index("idx_zones_active", table_name="zones")
    op.drop_column("zones", "is_active")

    # Step 4: Add new index
    op.create_index("idx_zones_phase", "zones", ["phase"])


def downgrade() -> None:
    """Reverse: drop phase + finalizers, restore is_active."""
    # Re-add is_active
    op.add_column(
        "zones",
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
    )

    # Migrate data back
    op.execute("UPDATE zones SET is_active = CASE WHEN phase = 'Active' THEN 1 ELSE 0 END")

    # Drop new columns and index
    op.drop_index("idx_zones_phase", table_name="zones")
    op.drop_column("zones", "finalizers")
    op.drop_column("zones", "phase")

    # Re-create old index
    op.create_index("idx_zones_active", "zones", ["is_active"])
