"""Merge approval decision queue (Issue #3790) with edge-permission schema repair.

This merge migration brings together two branches that diverged from
3b2a1c5d7e8f:

  - rename_closure_tenant_to_zone: our chain
      3b2a1c5d7e8f → add_approval_decision_queue → rename_closure_tenant_to_zone
  - c7d9a0f4b8e2: develop's chain
      ... → b6f4a8d9c2e1 → c7d9a0f4b8e2

Revision ID: merge_approval_and_edge_schema
Revises: rename_closure_tenant_to_zone, c7d9a0f4b8e2
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_approval_and_edge_schema"
down_revision: Union[str, Sequence[str], None] = (
    "rename_closure_tenant_to_zone",
    "c7d9a0f4b8e2",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
