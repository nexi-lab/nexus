"""Merge spending policy and contextual chunking heads.

Merges two independent branches that both descend from
merge_all_heads_for_test_harness:
- add_approval_rate_limit_rules (Issue #1358: Spending Policy Engine)
- add_contextual_chunk_fields (Issue #1192: Contextual Chunking)

Revision ID: merge_spending_and_contextual
Revises: add_approval_rate_limit_rules, add_contextual_chunk_fields
Create Date: 2026-02-14
"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_spending_and_contextual"
down_revision: Union[str, Sequence[str], None] = (
    "add_approval_rate_limit_rules",
    "add_contextual_chunk_fields",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge migration — no schema changes."""


def downgrade() -> None:
    """Merge migration — no schema changes."""
