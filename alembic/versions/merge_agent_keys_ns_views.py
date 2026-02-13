"""Merge agent_keys, namespace_views, and main heads.

Three branches diverged without a merge migration:
- a39d6aabdd22 (main chain)
- add_agent_keys_table (agent identity, Issue #1355)
- add_persistent_namespace_views (namespace cache, Issue #1265)

This merge resolves the MultipleHeads error in init_database.py / Docker CI.

Revision ID: merge_agent_keys_ns_views
Revises: a39d6aabdd22, add_agent_keys_table, add_persistent_namespace_views
Create Date: 2026-02-13
"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_agent_keys_ns_views"
down_revision: Union[str, Sequence[str], None] = (
    "a39d6aabdd22",
    "add_agent_keys_table",
    "add_persistent_namespace_views",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge-only migration — no schema changes."""


def downgrade() -> None:
    """Merge-only migration — no schema changes."""
