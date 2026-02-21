"""Merge agent_spec and zone_phase heads.

Revision ID: merge_agent_spec_zone_phase
Revises: add_agent_spec_col, add_zone_phase_finalizers
Create Date: 2026-02-20

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_agent_spec_zone_phase"
down_revision: Union[str, Sequence[str], None] = (
    "add_agent_spec_col",
    "add_zone_phase_finalizers",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge heads — no schema changes."""
    pass


def downgrade() -> None:
    """Downgrade — no schema changes."""
    pass
